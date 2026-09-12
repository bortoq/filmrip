#!/usr/bin/env python3
"""Smart movie compression to AV1 (video) + Opus (audio) at minimal bitrate.

Use it through the mkv / mkvf wrappers in ~/bin:
    mkvf film.mkv      # film:      --vmaf 94 --sdr 72
    mkv  cartoon.mkv   # animation: --vmaf 90 --sdr 60

Or call directly with subjective quality targets:
    mkv_encode.py --vmaf 95 --sdr 90 film.mkv

How it works:
    audio: for each track, a 60-second sample from the middle of the film
           is used to find the lowest Opus bitrate with SI-SDR >= target
           (--sdr 0..100 maps linearly to 0..25 dB; see README table);
    video: ab-av1 crf-search finds the highest CRF with VMAF >= --vmaf,
           then ab-av1 encode compresses the whole file.

Screen output is kept minimal: one updating audio-search line (ab-av1
style), then the normal ab-av1 crf-search / encode progress.
"""

import argparse
import atexit
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

PRESET = 3
SAMPLE_SECS = 60  # audio sample length for bitrate search, in seconds
CACHE_DIR = "/tmp"

# --sdr 100 maps to this SI-SDR (dB). Around here Opus is effectively
# transparent for film audio (~96 kbps per channel).
SISDR_AT_100 = 25.0

# Candidate total Opus bitrates (per track), in kbit/s.
BITRATE_LADDER = [16, 24, 32, 40, 48, 56, 64, 72, 80, 96, 112, 128,
                  160, 192, 224, 256, 320, 384, 448, 512]

LOSSLESS_CODECS = {"flac", "alac", "mlp", "truehd",
                   "pcm_s16le", "pcm_s24le", "pcm_s32le",
                   "pcm_f32le", "pcm_f64le", "pcm_u8", "pcm_s16be",
                   "pcm_s24be", "pcm_s32be"}

# Fallback if sampling / measuring fails (~24 kbps per channel).
STATIC_FALLBACK = {1: 24, 2: 48, 6: 144, 7: 168, 8: 192}

# Temp-dir prefixes created by this script (cleaned on start / exit).
_TMP_PREFIXES = ("mkv_audio_", "mkv_abrest_")

# Paths and child processes to drop on abort / exit.
_CLEANUP_FILES = set()
_CLEANUP_DIRS = set()
_CHILD_PROCS = []
_CLEANING = False


def eprint(msg):
    print(msg, file=sys.stderr)


def sdr_to_sisdr(sdr_percent):
    """Map subjective --sdr (0..100) to a SI-SDR target in dB."""
    return float(sdr_percent) / 100.0 * SISDR_AT_100


def _register_file(path):
    if path:
        _CLEANUP_FILES.add(path)


def _unregister_file(path):
    _CLEANUP_FILES.discard(path)


def _register_dir(path):
    if path:
        _CLEANUP_DIRS.add(path)


def _unregister_dir(path):
    _CLEANUP_DIRS.discard(path)


def _kill_children():
    for p in list(_CHILD_PROCS):
        try:
            p.kill()
        except Exception:
            pass
        try:
            p.wait(timeout=5)
        except Exception:
            pass
    _CHILD_PROCS.clear()


def cleanup_generated(quiet=True):
    """Remove temp files/dirs produced by this run; kill child processes."""
    global _CLEANING
    if _CLEANING:
        return
    _CLEANING = True
    try:
        _kill_children()
        for path in list(_CLEANUP_FILES):
            try:
                if os.path.isfile(path) or os.path.islink(path):
                    os.remove(path)
            except OSError:
                if not quiet:
                    eprint(f"Cannot remove {path}")
            _CLEANUP_FILES.discard(path)
        for path in list(_CLEANUP_DIRS):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass
            _CLEANUP_DIRS.discard(path)
    finally:
        _CLEANING = False


def cleanup_stale_temps(input_path=None):
    """Drop leftovers from a previous killed / power-loss run."""
    if input_path:
        base, ext = os.path.splitext(os.path.realpath(input_path))
        stale = base + "_tmp_encode" + ext
        if os.path.isfile(stale):
            try:
                os.remove(stale)
            except OSError as e:
                eprint(f"Cannot remove stale temp file {stale}: {e}")
                return False
    tmp_root = tempfile.gettempdir()
    try:
        names = os.listdir(tmp_root)
    except OSError:
        return True
    for name in names:
        if not any(name.startswith(p) for p in _TMP_PREFIXES):
            continue
        path = os.path.join(tmp_root, name)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
    return True


def _on_signal(signum, frame):
    cleanup_generated()
    # 128 + signal number, same convention as shells.
    sys.exit(128 + (signum if isinstance(signum, int) else 0))


def run_quiet(cmd):
    """Run a command with no screen output. Returns CompletedProcess."""
    return subprocess.run(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)


def probe(path):
    """ffprobe: duration + audio track list. Guards all parsing."""
    if not path or not os.path.isfile(path):
        return 0.0, []
    r = run_quiet(["ffprobe", "-v", "error", "-show_streams",
                   "-show_format", "-of", "json", path])
    if r.returncode != 0:
        return 0.0, []
    try:
        info = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return 0.0, []
    try:
        duration = float(info.get("format", {}).get("duration") or 0.0)
    except (ValueError, TypeError):
        duration = 0.0
    tracks = []
    a_idx = 0
    for s in info.get("streams", []) or []:
        if s.get("codec_type") != "audio":
            continue
        try:
            ch = int(s.get("channels") or 0)
        except (ValueError, TypeError):
            ch = 0
        br = None
        try:
            br = int(s.get("bit_rate") or 0) or None
        except (ValueError, TypeError):
            br = None
        tags = s.get("tags") or {}
        tracks.append({
            "aindex": a_idx,  # audio-only index (for b:a:N / map)
            "channels": ch,
            "codec": (s.get("codec_name") or "unknown").lower(),
            "bit_rate": br,  # bits per second, or None
            "lang": tags.get("language") or tags.get("LANGUAGE") or "?",
        })
        a_idx += 1
    return duration, tracks


def bitrate_bounds(track):
    """Allowed total bitrate range for a track, in kbit/s."""
    ch = track.get("channels") or 0
    if ch <= 0:
        return 32, 128
    lo = max(16, ch * 12)
    hi = min(512, ch * 96)
    # A lossy source cannot get better: never use more bits than the original.
    br = track.get("bit_rate")
    codec = track.get("codec") or ""
    if br and codec not in LOSSLESS_CODECS and codec != "opus":
        hi = min(hi, max(lo, math.ceil(br / 1000)))
    return lo, hi


def estimate_src_bitrate(path, aindex, start, dur):
    """Guess the source track bitrate (bits per second).

    ffprobe often reports audio bitrate in MKV as N/A. Without a guess,
    the search would use the full range and could "upscale" a lossy source.
    A fast stream-copy of the 60-second sample gives a fair guess.
    Returns None if the guess fails.
    """
    if not path or dur is None or dur <= 0:
        return None
    try:
        tmp = tempfile.mkdtemp(prefix="mkv_abrest_")
        _register_dir(tmp)
        try:
            sample = os.path.join(tmp, "src.mka")
            # -ss/-t AFTER -i (output seeking): input seeking with
            # stream-copy gives wrong length / broken files.
            r = run_quiet(["ffmpeg", "-y", "-v", "error", "-i", path,
                           "-ss", f"{start:.1f}", "-t", f"{dur:.1f}",
                           "-map", f"0:a:{aindex}",
                           "-c", "copy", sample])
            if r.returncode != 0 or not os.path.isfile(sample):
                return None
            size = os.path.getsize(sample)
            if size <= 0:
                return None
            return int(size * 8 / dur)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            _unregister_dir(tmp)
    except Exception:
        return None


def ladder_for(lo, hi):
    """Standard ladder clipped to [lo, hi]; always at least 2 steps."""
    cands = [b for b in BITRATE_LADDER if lo <= b <= hi]
    if not cands:
        cands = sorted({lo, hi})
    elif len(cands) == 1:
        cands = sorted({cands[0], hi} if hi != cands[0] else {cands[0], lo})
    if len(cands) < 2:
        # lo == hi sitting right on the ladder: borrow the nearest
        # ladder neighbor so the search always has two points.
        for b in BITRATE_LADDER:
            if b not in cands:
                cands = sorted(cands + [b])
                break
    return cands


def extract_sample(path, aindex, start, dur, out_wav, channels=0):
    """Cut a track sample to 48 kHz wav, keeping the channel count.

    6-channel tracks are normalized to 5.1: the Opus encoder rejects
    the 5.1(side) layout, and channel reordering does not change the score.
    The same normalization is used in the final encode.
    """
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.1f}",
           "-i", path, "-map", f"0:a:{aindex}", "-t", f"{dur:.1f}",
           "-ar", "48000"]
    if channels == 6:
        cmd += ["-af", "channelmap=channel_layout=5.1"]
    cmd += ["-c:a", "pcm_s16le", out_wav]
    r = run_quiet(cmd)
    return r.returncode == 0 and os.path.isfile(out_wav) and \
        os.path.getsize(out_wav) > 0


def encode_opus(ref_wav, bitrate_k, out_opus):
    r = run_quiet(["ffmpeg", "-y", "-v", "error", "-i", ref_wav,
                   "-c:a", "libopus", "-b:a", f"{bitrate_k}k",
                   "-application", "audio", out_opus])
    return r.returncode == 0 and os.path.isfile(out_opus)


def active_channels(ref_wav, thresh_db=-60.0):
    """0-based indexes of reference channels with energy above the limit.

    Silent (or near silent) channels, such as LFE pauses, are useless
    for SI-SDR: tiny encoder noise scores -40 dB there and drags the
    average down. Returns None if energy cannot be measured.
    """
    if not ref_wav or not os.path.isfile(ref_wav):
        return None
    r = run_quiet(["ffmpeg", "-i", ref_wav, "-lavfi", "astats=metadata=0",
                   "-f", "null", "-"])
    rms = []
    cur = None
    for line in (r.stderr or "").splitlines():
        m = re.search(r"Channel:\s*(\d+)", line)
        if m:
            cur = int(m.group(1)) - 1
            continue
        m = re.search(r"RMS level dB:\s*(-?inf|nan|-?\d+(?:\.\d+)?)", line)
        if m and cur is not None and cur == len(rms):
            try:
                rms.append(float(m.group(1)))
            except ValueError:
                rms.append(float("-inf"))
    if not rms:
        return None
    return [i for i, v in enumerate(rms)
            if not math.isnan(v) and v > thresh_db]


def measure_sisdr(ref_wav, enc_file, active=None):
    """Average SI-SDR (dB) over the loud channels.

    active is a channel index list from active_channels(). A silent track
    (active == []) is clear at any bitrate (+inf).
    """
    if not ref_wav or not enc_file:
        return None
    if not os.path.isfile(ref_wav) or not os.path.isfile(enc_file):
        return None
    r = run_quiet(["ffmpeg", "-i", ref_wav, "-i", enc_file,
                   "-lavfi", "[0:a][1:a]asisdr", "-f", "null", "-"])
    vals = re.findall(r"SI-SDR[^:]*:\s*(-?inf|nan|-?\d+(?:\.\d+)?)",
                      r.stderr or "")
    if not vals:
        return None
    try:
        nums = [float(v) for v in vals]
    except ValueError:
        return None
    if active is not None:
        if not active:
            return float("inf")  # silence is clear at any bitrate
        nums = [nums[i] for i in active
                if i < len(nums) and not math.isnan(nums[i])]
    else:
        nums = [v for v in nums if not math.isnan(v)]
    if not nums:
        return None
    return sum(nums) / len(nums)


def _json_safe_score(score):
    """Round a score for print/cache; non-finite becomes None.

    JSON cannot store Infinity/NaN, so silence (+inf) is reported
    without a number instead of poisoning a future cache file.
    """
    if score is None or not math.isfinite(score):
        return None
    return round(score, 1)


class _AudioProgress:
    """Single-line ab-av1-style progress for the audio bitrate search.

    ab-av1 encode prints:  Encoded 56.82 KiB (103%)\\r
    We mirror that shape:  audio a0 48k 12.3dB (40%)\\r
    """

    def __init__(self, total_steps):
        self.total = max(1, int(total_steps))
        self.done = 0
        self._last_len = 0
        self._finished = False

    def update(self, aindex=None, bitrate_k=None, score=None):
        self.done = min(self.done + 1, self.total)
        pct = int(100 * self.done / self.total)
        parts = ["audio"]
        if aindex is not None:
            parts.append(f"a{aindex}")
        if bitrate_k is not None:
            parts.append(f"{bitrate_k}k")
        if score is not None and math.isfinite(score):
            parts.append(f"{score:.1f}dB")
        parts.append(f"({pct}%)")
        line = " ".join(parts)
        pad = max(0, self._last_len - len(line))
        sys.stderr.write("\r" + line + (" " * pad))
        sys.stderr.flush()
        self._last_len = len(line)

    def finish(self):
        if self._finished:
            return
        self._finished = True
        if self._last_len:
            sys.stderr.write("\r" + (" " * self._last_len) + "\r")
            sys.stderr.flush()


def pick_audio_bitrate(path, track, target_db, duration, progress=None):
    """Lowest Opus bitrate for the target SI-SDR.

    Returns (bitrate_kbit, sisdr_db|None, method), where method is:
    copy — opus is already good enough, copy it; search — sample search;
    max — target not reached even at the ceiling; fallback — static table.
    """
    ch = track.get("channels") or 0
    codec = track.get("codec") or ""
    src_br = track.get("bit_rate")
    lo, hi = bitrate_bounds(track)
    cands = ladder_for(lo, hi) if ch > 0 else [64]
    steps_left = max(1, len(cands))

    def tick(bitrate_k=None, score=None, consume=1):
        nonlocal steps_left
        if not progress or steps_left <= 0:
            return
        n = min(max(int(consume), 0), steps_left)
        if n <= 0:
            return
        for _ in range(n):
            progress.update(aindex=track.get("aindex"),
                            bitrate_k=bitrate_k, score=score)
        steps_left -= n

    if duration <= 0:
        duration = 120.0
    sample_dur = min(float(SAMPLE_SECS), max(10.0, duration * 0.05))
    start = max(0.0, duration * 0.33 - sample_dur / 2)

    # Without the SI-SDR meter there is no honest search: use the static
    # table instead of a blind ceiling.
    if run_quiet(["ffmpeg", "-hide_banner", "-h",
                  "filter=asisdr"]).returncode != 0:
        tick(bitrate_k=STATIC_FALLBACK.get(ch, 64), consume=steps_left)
        return STATIC_FALLBACK.get(ch, 64), None, "fallback"

    # Bitrate unknown (normal for audio in MKV): guess it with a fast
    # sample copy, so a lossy source is never "upscaled".
    # Lossless guesses are always above the search ceiling, so no cap there.
    est_br = None
    if src_br is None and codec not in LOSSLESS_CODECS:
        est_br = estimate_src_bitrate(path, track["aindex"], start,
                                      sample_dur)
        if est_br:
            hi = min(hi, max(lo, math.ceil(est_br / 1000)))
            cands = ladder_for(lo, hi)
            steps_left = max(1, len(cands))

    eff_br = src_br if src_br else est_br
    # Opus that is already good enough (bitrate at or below need) — copy it.
    if codec == "opus" and eff_br:
        if math.ceil(eff_br / 1000) <= hi:
            tick(bitrate_k=math.ceil(eff_br / 1000), consume=steps_left)
            return None, None, "copy"

    if ch <= 0:
        tick(consume=steps_left)
        return 64, None, "fallback"

    tmp = None
    try:
        tmp = tempfile.mkdtemp(prefix="mkv_audio_")
        _register_dir(tmp)
        ref = os.path.join(tmp, "ref.wav")
        if not extract_sample(path, track["aindex"], start,
                              sample_dur, ref,
                              channels=track.get("channels") or 0):
            raise RuntimeError("sample")
        active = active_channels(ref)
        for br in cands:
            enc = os.path.join(tmp, f"enc_{br}.opus")
            if not encode_opus(ref, br, enc):
                tick(bitrate_k=br)
                continue
            score = measure_sisdr(ref, enc, active)
            if score is None:
                tick(bitrate_k=br)
                continue
            if score >= target_db:
                tick(bitrate_k=br, score=score, consume=steps_left)
                if eff_br and eff_br <= br * 1000:
                    return None, None, "copy"
                return br, _json_safe_score(score), "search"
            tick(bitrate_k=br, score=score)
        # Target not reached. But if the source is already at the
        # ceiling, copy it: same bytes with no re-encode loss.
        if eff_br and eff_br <= cands[-1] * 1000:
            tick(bitrate_k=cands[-1], consume=steps_left)
            return None, None, "copy"
        # Else take the ceiling (closest to the target).
        enc = os.path.join(tmp, f"enc_{cands[-1]}.opus")
        score = None
        if encode_opus(ref, cands[-1], enc):
            score = measure_sisdr(ref, enc, active)
        tick(bitrate_k=cands[-1], score=score, consume=steps_left)
        return cands[-1], _json_safe_score(score), "max"
    except Exception:
        tick(consume=steps_left)
        fb = STATIC_FALLBACK.get(ch, 64)
        return fb, None, "fallback"
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
            _unregister_dir(tmp)


def _estimate_audio_steps(tracks, duration):
    """Rough step count for the progress bar (one tick per candidate)."""
    if not tracks:
        return 1
    total = 0
    for t in tracks:
        lo, hi = bitrate_bounds(t)
        total += max(1, len(ladder_for(lo, hi)))
    return total


def run_crf_search(path, min_vmaf):
    """ab-av1 crf-search; returns the CRF (float).

    stderr is inherited so ab-av1 keeps its TTY progress bar. stdout is
    captured as NDJSON (--stdout-format json) to read the final CRF
    without stealing the terminal from the progress UI.
    """
    if not path or min_vmaf is None:
        return None
    cmd = ["ab-av1", "crf-search", "-i", path, "--preset", str(PRESET),
           "--min-vmaf", str(min_vmaf), "--stdout-format", "json"]
    try:
        # stderr=None → inherit the real terminal (progress bar).
        # Piping stderr makes ab-av1 fall back to INFO log lines.
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=None, text=True, bufsize=1)
    except FileNotFoundError:
        return None
    _CHILD_PROCS.append(p)
    found = None
    try:
        for line in p.stdout:
            line = (line or "").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "crf-search-done":
                try:
                    found = float(msg["crf"])
                except (KeyError, TypeError, ValueError):
                    pass
        p.wait()
    except BaseException:
        # Never leave an orphan encoder burning CPU on abort.
        try:
            p.kill()
        except Exception:
            pass
        raise
    finally:
        try:
            p.stdout.close()
        except Exception:
            pass
        if p in _CHILD_PROCS:
            _CHILD_PROCS.remove(p)
    if p.returncode != 0:
        return None
    return found


def load_cache(cache_path):
    """Read (crf, audio_plan) from a cache file; (None, None) if bad.

    crf may be null when only the audio plan was saved so far.
    /tmp is world-writable: every entry is validated so a broken or
    planted cache file cannot crash us.
    """
    try:
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        raw_crf = cached.get("crf")
        crf = float(raw_crf) if raw_crf is not None else None
        if crf is not None and not math.isfinite(crf):
            raise ValueError("bad crf")
        raw_plan = cached.get("audio", [])
        if not isinstance(raw_plan, list):
            raise ValueError("bad audio plan")
        plan = []
        for a in raw_plan:
            if (not isinstance(a, (list, tuple)) or len(a) != 2
                    or (a[0] is not None and not isinstance(a[0], int))
                    or a[1] not in ("copy", "search", "max", "fallback")):
                raise ValueError("bad cache entry")
            plan.append((a[0], a[1]))
        return crf, plan
    except (OSError, ValueError, TypeError, KeyError,
            IndexError, AttributeError):
        return None, None


def main():
    # Never crash on non-UTF8 locales: replace bad glyphs instead.
    for _stream in (sys.stderr, sys.stdout):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    atexit.register(cleanup_generated)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass

    ap = argparse.ArgumentParser(
        description="Compress a movie: AV1 video (--vmaf) + Opus audio "
                    "(--sdr subjective quality 0..100).")
    ap.add_argument("input", nargs="?", help="input video file")
    ap.add_argument("--vmaf", type=float, default=94.0,
                    help="target video VMAF (default: 94)")
    ap.add_argument("--sdr", type=float, default=72.0,
                    help="subjective audio quality 0..100 "
                         "(maps to SI-SDR; default: 72 ≈ 40 kbps/ch)")
    args = ap.parse_args()

    if not args.input:
        eprint(f"Usage: {os.path.basename(sys.argv[0])} "
               f"[--vmaf N] [--sdr N] input.mkv")
        return 1
    if not os.path.isfile(args.input):
        eprint(f"File not found: {args.input}")
        return 1
    if not (0.0 <= args.sdr <= 100.0):
        eprint("--sdr must be between 0 and 100")
        return 1
    if args.vmaf <= 0:
        eprint("--vmaf must be positive")
        return 1

    min_vmaf = args.vmaf
    min_sisdr = sdr_to_sisdr(args.sdr)

    abs_input = os.path.realpath(args.input)
    filename = os.path.basename(abs_input)
    try:
        filesize = os.path.getsize(abs_input)
    except OSError as e:
        eprint(f"Cannot read file: {e}")
        return 1

    if not cleanup_stale_temps(abs_input):
        return 1

    cache_id = (f"{filename}.{filesize}b."
                f"vmaf{min_vmaf}.sdr{args.sdr}")
    cache_path = os.path.join(CACHE_DIR, cache_id + ".json")
    # Legacy caches from older flag names / bash wrapper.
    legacy_cache = os.path.join(
        CACHE_DIR,
        f"{filename}.{filesize}b.vmaf{min_vmaf}.sisdr{min_sisdr}.json")
    legacy_crf = os.path.join(CACHE_DIR,
                              f"{filename}.{filesize}b.vmaf{min_vmaf}.crf")

    base, ext = os.path.splitext(abs_input)
    tmp_output = base + "_tmp_encode" + ext
    bak_file = abs_input + ".bak"

    if os.path.exists(bak_file):
        try:
            ans = input(f"Backup file already exists: {bak_file}\n"
                        f"Overwrite the existing .bak with the new original? "
                        f"[y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 1
        if ans not in ("y", "yes"):
            return 1

    # --- cache ---
    crf, audio_plan = None, None
    if os.path.isfile(cache_path):
        crf, audio_plan = load_cache(cache_path)
    elif os.path.isfile(legacy_cache):
        crf, audio_plan = load_cache(legacy_cache)

    duration, tracks = probe(abs_input)

    # Cache from other tracks (same name/size, other streams) —
    # the audio plan is useless, search again.
    if audio_plan is not None and len(audio_plan) != len(tracks):
        audio_plan = None

    # --- 1. audio: bitrates (first — so the user sees this before ab-av1) ---
    if audio_plan is None:
        steps = _estimate_audio_steps(tracks, duration)
        progress = _AudioProgress(steps) if tracks else None
        audio_plan = []
        try:
            for t in tracks:
                br, score, method = pick_audio_bitrate(
                    abs_input, t, min_sisdr, duration, progress=progress)
                audio_plan.append((br, method))
        finally:
            if progress:
                progress.finish()
        # Persist audio plan early (crf may still be unknown).
        try:
            with open(cache_path + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"crf": crf, "audio": audio_plan}, f)
            os.replace(cache_path + ".tmp", cache_path)
        except OSError:
            pass

    # --- 2. video: CRF ---
    if crf is None:
        if os.path.isfile(legacy_crf):
            try:
                with open(legacy_crf, encoding="utf-8") as f:
                    crf = float(f.read().strip())
            except (OSError, ValueError):
                crf = None
        if crf is None:
            crf = run_crf_search(abs_input, min_vmaf)
            if crf is None:
                eprint("Video check failed (CRF search)!")
                return 1
        try:
            with open(cache_path + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"crf": crf, "audio": audio_plan}, f)
            os.replace(cache_path + ".tmp", cache_path)
        except OSError:
            pass
        try:
            with open(legacy_crf, "w", encoding="utf-8") as f:
                f.write(str(crf))
        except OSError:
            pass

    # --- 3. free disk space ---
    try:
        avail = shutil.disk_usage(os.path.dirname(abs_input)).free
        if avail < filesize:
            eprint(f"Not enough free space near the file "
                   f"({os.path.dirname(abs_input)}): free "
                   f"{avail // 1024} KB, need ~{filesize // 1024} KB.")
            return 1
    except OSError:
        pass

    # --- 4. encode ---
    _register_file(tmp_output)
    cmd = ["ab-av1", "encode", "-i", abs_input, "-o", tmp_output,
           "--crf", str(crf), "--preset", str(PRESET)]
    if tracks:
        cmd += ["--acodec", "libopus"]
        for t, (br, method) in zip(tracks, audio_plan):
            idx = t["aindex"]
            if method == "copy":
                cmd += ["--enc", f"c:a:{idx}=copy"]
                continue
            if (t["channels"] or 0) == 6:
                # True 6-channel tracks: 5.1(side) -> 5.1,
                # or the Opus mapping breaks on the side layout.
                cmd += ["--enc", f"mapping_family:a:{idx}=1"]
                cmd += ["--enc",
                        f"filter:a:{idx}=channelmap=channel_layout=5.1"]
            cmd += ["--enc", f"b:a:{idx}={br}k"]

    try:
        p = subprocess.Popen(cmd)
    except FileNotFoundError:
        eprint("ab-av1 not found. Install ab-av1 and ffmpeg.")
        cleanup_generated()
        return 1
    _CHILD_PROCS.append(p)
    try:
        rc = p.wait()
    except BaseException:
        try:
            p.kill()
        except Exception:
            pass
        cleanup_generated()
        raise
    finally:
        if p in _CHILD_PROCS:
            _CHILD_PROCS.remove(p)

    if rc != 0:
        cleanup_generated()
        eprint("Encode failed! Input file untouched.")
        return 1

    try:
        os.replace(abs_input, bak_file)
        os.replace(tmp_output, abs_input)
        _unregister_file(tmp_output)
    except OSError as e:
        eprint(f"Error replacing files: {e}")
        cleanup_generated()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
