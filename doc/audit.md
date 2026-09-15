# Audit: mkv (smart AV1 + Opus compression)

- Date: 2026-09-11
- Reviewer: Auditor (Senior Code Forensics / AppSec QA)
- Target: `mkv_encode.py` (553 lines), `mkv` / `mkvf` wrappers, `README.md`
- Commit: `f372431` ("Smart AV1+Opus compression with per-track audio bitrate search")

## Verdict: [APPROVED] (after re-check)

Reason: no tests (checklist item 5 — automatic REJECT) + one
critical reliability defect (crash on a damaged cache). Details in
the table.

| Code block | Type | Detail | Fix |
|---|---|---|---|
| whole repo | Crit | No tests for any function. Checklist item 5 demands tests, without them — REJECT | Add `tests/` with unit tests for pure functions (`bitrate_bounds`, `ladder_for`, `probe` on ffprobe-json fixtures) and `subprocess`-mocked tests for `estimate_src_bitrate`, `active_channels`, `measure_sisdr`, `pick_audio_bitrate`, `run_crf_search`, `main` argument validation. Minimum: `pytest`, one command to run |
| `main`, lines 412–418 (cache read) | Crit | Damaged/planted JSON in world-writable `/tmp` crashes the script with a traceback: with `audio` as string/dict/short list, the list comprehension `[(a[0], a[1]) ...]` throws `IndexError`, but only `(OSError, ValueError, TypeError, KeyError)` is caught. Verified live: `'"ab"'`, `'{"x": 1}'`, `'[["a"]]'` → `IndexError` | Add `IndexError` (and `AttributeError`) to except, or validate entry shape: `if isinstance(a, (list, tuple)) and len(a) == 2` |
| `main`, step 4 (encode) | Warning | Leftover `_tmp_encode` from a killed run (`SIGKILL`, power loss) is not cleaned at start — cleanup exists only on fail/interrupt paths. The next `ab-av1 encode` may refuse to write into an existing file | After the `.bak` confirmation, before encoding: if `tmp_output` exists — delete it (or ask). Mirror of the old `trap ... rm -f` |
| `run_crf_search`, lines 323–353 | Warning | On `KeyboardInterrupt`/exception while reading `p.stdout` the child `ab-av1` is not stopped (`p.kill()` missing) — an orphan encoder burns CPU. `p.stdout.close()` in `finally` does not stop the child | Wrap the loop in `try/finally` with `p.kill(); p.wait()` on abnormal exit |
| `eprint` + all messages | Warning | Non-ASCII marks `→`, `—` in strings (lines 467, 472 and others). Under a non-UTF-8 locale (`LANG=C`, ASCII stderr) any `print` of such a message throws `UnicodeEncodeError` and masks the real error | Either ASCII-only output, or `sys.stderr.reconfigure(errors="replace")` at start |
| `pick_audio_bitrate`, max branch | Warning | If the ffmpeg build lacks the `asisdr` filter, `measure_sisdr` returns `None` for ALL candidates — the code silently falls to the ceiling (`max`) instead of an honest `fallback`. The user sees "ceiling" instead of "cannot measure" | Check the filter once (`ffmpeg -h filter=asisdr`) and go straight to `fallback` |
| two `os.rename` in a row, lines 540–541 | Warning | The swap is not atomic: a crash between renames leaves the original only in `.bak` (recoverable by hand, but silently) | Acceptable as is; minimum — a README line about manual recovery from `.bak`. Better: `os.replace` + fsync dir (opt) |
| `ladder_for`, lines 152–159 | Opt | Docstring promises "at least 2 steps", but with `lo == hi` inside the ladder (`ladder_for(64, 64)` → `[64]`, verified) it returns 1. Harmless for correctness | Fix the docstring or the code |
| `pick_audio_bitrate`, line 317 | Opt | Bare `except Exception: pass` hides real bugs (though it honestly falls to `fallback`) | At least `eprint(f"audio search failed: {e}")` to stderr |
| `measure_sisdr` + `round(float('inf'), 1)` | Opt | A fully silent track prints "SI-SDR inf dB". It never reaches JSON (only `br`/`method` are cached), but it is fragile: start caching `score` and the JSON becomes invalid (`Infinity`) | Guard comment or `score = None if score == inf` before caching |
| `doc/roadmap.md` | Opt | File missing — check against spec (checklist item 6) impossible | Create `doc/roadmap.md` or drop the item from the pipeline |
| repo | Opt | No `.gitignore`; `python3 -m py_compile` / runs create `__pycache__/`, easy to commit by mistake | Add `.gitignore` with `__pycache__/` |

## Checked and accepted

1. **Memory/resources.** No leaks: samples live in `TemporaryDirectory`
   (auto cleanup); `p.stdout.close()` in `finally`; `tmp_output` is
   removed on fail/Ctrl+C paths (except the stale-file note above).
   Double-free / use-after-free do not apply (Python). `Popen`
   descriptors are closed.
2. **Security.** `shell=True`, `os.system`, `eval/exec`, `pickle` —
   absent (verified by `grep`). All runs are argument lists with no
   shell, injection through file/track names impossible. Paths go
   through `realpath`, cache writes are atomic (`.tmp` + `os.replace`),
   no secrets in cache (numbers only). Tmp files use
   `TemporaryDirectory` (0700).
3. **Complexity.** Audio search is `O(T × C)` short encodes of a 60s
   sample (T — tracks, C — ladder steps, bottom-up search = minimal
   bytes); next to video `crf-search` (minutes) — negligible. No
   extra allocations.
4. **Readability.** Naming, docstrings, comments — good; guard clauses
   in place; `active_channels` logic (limit −60 dB, guard against
   `astats` total lines via `cur == len(rms)`) — correct, confirmed
   by a run (`[0, 1]` on a 5.1 sample). Mixed Unicode in output —
   see Warning above.
5. **Behavior confirmed by earlier runs:** stereo 80k / 5.1 256k
   (film), 64k / 192k (animation); opus 48k → `copy` end to end;
   `.bak` prompt with `n` — clean exit 1; file with no audio — OK;
   `5.1(side)` — OK.
6. **ROADMAP.** `doc/roadmap.md` missing — check impossible (see Opt).

## What the Coder had to do

1. Add `tests/` + `pytest` (Crit, blocker).
2. Fix cache reading — `IndexError`/shape validation (Crit, blocker).
3. Nice to have: stale-tmp cleanup at start, `p.kill()` in
   `run_crf_search`, ASCII-safe output or `errors="replace"`,
   `asisdr` presence check, `.gitignore`, `doc/roadmap.md`.

After fixes — return for re-audit.

## Re-audit: 2026-09-11

### Re-audit result: [APPROVED]

Run: `python3 -m pytest tests/ -q` → **54 passed**.
Plus live checks through real functions (not mocks):
`load_cache` stable against 9 broken payloads — OK,
good cache reads — OK, `ladder_for` always ≥ 2 steps — OK,
`asisdr` present in the build, `_json_safe_score` — OK.

| First-audit item | Status | Proof |
|---|---|---|
| Tests (Crit) | Closed | `tests/test_mkv_encode.py`, 54 tests per function: `eprint`, `run_quiet`, `probe`, `bitrate_bounds`, `ladder_for`, `estimate_src_bitrate`, `extract_sample`, `encode_opus`, `active_channels`, `measure_sisdr`, `_json_safe_score`, `load_cache`, `pick_audio_bitrate` (copy/search/max/fallback), `run_crf_search` (parsing, `kill`), `main` validation. One command, no external files/network |
| Crash on bad cache (Crit) | Closed | Reading moved to `load_cache()` with per-entry validation + `IndexError`/`AttributeError` in except. All first-audit payloads (`'"ab"'`, `'{"x": 1}'`, `'[["a"]]'` and more) return `(None, None)` — verified live |
| Stale `_tmp_encode` (Warning) | Closed | Leftover removed after the `.bak` confirmation, error → exit 1 |
| Orphan `ab-av1` (Warning) | Closed | `except BaseException` → `p.kill()` → `raise`; `kill` call covered by a test |
| Unicode in ASCII locale (Warning) | Closed | `stderr`/`stdout` → `errors="replace"` at the start of `main` |
| No `asisdr` → blind ceiling (Warning) | Closed | `ffmpeg -h filter=asisdr` check; without the filter — honest `fallback` (covered by a test) |
| Non-atomic swap (Warning) | Closed (minimum) | `os.rename` → `os.replace` + recovery-from-`.bak` line in README |
| `ladder_for` / docstring (Opt) | Closed | Code really guarantees ≥ 2 steps (ladder-neighbour borrow); verified by test and live |
| Bare `except` (Opt) | Closed | Reason goes to stderr before fallback (verified by a `capsys` test) |
| `inf` in score (Opt) | Closed | `_json_safe_score()`: non-finite → `None`; used in both branches |
| `.gitignore` (Opt) | Closed | `__pycache__/`, `*.pyc`, `.pytest_cache/` |
| `doc/roadmap.md` (Opt) | Open, non-blocking | Still missing; creating it is Architect work, Coder rightfully untouched |

No new defects from the fixes:
no injections/shell, all calls are argument lists;
`_stream.reconfigure` in a narrow except; stale-tmp removal sits after
the `.bak` prompt and before cache read; the `asisdr` check has no
globals (one fast call per track).

## Full audit: 2026-09-14

- Scope: `mkv_encode.py` (2596 lines), `tests/test_mkv_encode.py`
  (135 tests), `README.md`, `doc/roadmap.md`, `doc/research.md`.
- Run: `python3 -m pytest tests/ -q` → **135 passed**;
  `pyflakes mkv_encode.py` → clean.
- Verdict: **[APPROVED]** with two findings fixed on the spot
  (dead assignment, cache `bool` CRF).

| Code block | Type | Detail | Fix |
|---|---|---|---|
| refine bisection (`br = cands[ans]`) | Opt | dead assignment found by pyflakes | removed, suite still green |
| `load_cache`, CRF parse | Warning | planted `{"crf": true}` read as 1.0 (`bool` is `int`) | reject `bool` CRF, test added |
| `load_cache` bad shapes | OK | all bad payloads → `(None, None, None)`; `Infinity`/`NaN` rejected | covered by tests |
| subprocess calls | OK | no `shell=True`, `os.system`, `eval`, `pickle`; every call is an argument list, so file names cannot inject | verified by grep |
| `/tmp` cache planting | OK | every entry validated; unknown methods/shapes rejected | covered by tests |
| parallel runs | OK | shared sweeps skip entries under one hour old (`_is_stale`); own-run dirs go through the registry, not the sweep | covered by tests |
| child processes | OK | own session per child, TERM then KILL, closing lists; abort paths kill the group | covered by tests |
| heartbeat threads | OK | daemon + join on `finish()`; every creation site finishes (audio loop, grain probe, copy wait) | covered by tests |
| terminal state | OK | cursor/colors/newline restored on every exit path, tty only | covered by tests |
| `except` clauses | OK | no bare `except`; broad catches return copy/fallback, never crash | verified by grep |
| math guards | OK | division by zero, non-finite, empty and missing inputs guarded in `size_percent`, `interp_br`, `percentile`, `pick_crop`, eta, cache | covered by tests |
| films read-only | OK | only reads touch the movie dir; writes go to system temp, output swap is atomic (`os.replace`) | verified by grep |

Memory/resources: no leaks found. Temp dirs use `mkdtemp` + registry
with cleanup on exit/signal/failure; pipes are drained by threads or
inherited; `stdout.close()` in `finally`.

Complexity: audio ≈ 30×5s rank probes + 3×8×10s refine probes per
track (minutes per film); grain race ≈ 27 fast 10s encodes (~1 min);
crop = 3 decodes (~30s); video `crf-search` dominates everything.

Readability: naming, guard clauses and docstrings are consistent;
one-letter temps stay inside small scopes. No dead code left
(pyflakes clean).

Tests: 135 pass in one command, no network, no real films needed.
Every public function has unit tests; new since last audit: rank +
bisect + median + exact interp, grain race, crop detect, spinner,
terminal restore, stale-guard, encode-failure diagnostics. Gaps kept
deliberately: real ab-av1/ffmpeg encodes (mocked; validated live on
real films instead — see below).

Roadmap compliance: verified item by item. Bar template, result
lines (`- sound N:`, `- algo, params (%)`, `- crop`, `- off`),
commas, no brackets, eta-only tails, percents as 100 * new / old,
same params to search and encode, cache keys over mode/level/crop,
copy-instead-of-upscale, transient misses not cached, instant
progress, terminal restore, read-only films, 1h stale guard,
30×5s rank + bisect + median + exact bitrate, flag-only `--noise`
with filter race and time cap, 3-sample crop consensus — all present
in code. One doc nit fixed during audit: stale `--noise [NAME]`
usage line → `[--noise]`.

Live validations on real films (not mocks): audio pick equals the
measured oracle (Bone Tomahawk 512k), exact 60k pick on stereo,
grain race picks on three films, crop consensus 1920:1072 confirmed
2877/2877 frames, removegrain wiring through ab-av1, OOM-kill
diagnosis from system journal.
