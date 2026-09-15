# Video denoise / film grain research

Notes from exploring how to strip film grain before AV1 encode and
restore it with AV1 film-grain synthesis. Plain English. We will edit
this file as we try more options.

## Goal

Film grain (and sensor noise) is almost random. Codecs spend a lot of
bits trying to encode it. AV1 can:

1. remove grain from the picture that gets encoded,
2. store a small grain model in the bitstream,
3. have the decoder paint similar grain back on playback.

We want that pipeline in `mkv_encode.py` when it helps size, without
wrecking detail or making encodes much slower.

## What ab-av1 already supports

ab-av1 does **not** auto-detect grain. It also has no dedicated
"denoise" command. It does expose the hooks we need:

| Hook | Flag | Role |
|------|------|------|
| Any ffmpeg video filter | `--vfilter "..."` | e.g. `hqdn3d`, `atadenoise`, `removegrain`, `fftdnoiz` before encode (and by default before VMAF) |
| SVT-AV1 extra params | `--svt key=value` | e.g. `--svt film-grain=8` |
| SVT built-in denoise | `--svt film-grain-denoise=1` | Wiener denoise inside SVT; strength follows `film-grain` |

Help even documents `--svt film-grain=8` as an example.

So we do **not** need a custom encode path to get AV1 film-grain
synthesis. We mainly decide *when* and *how hard* to enable it.

## Two approaches

### A. SVT only (current `--denoise` default)

```
--svt film-grain=N --svt film-grain-denoise=1
```

- SVT denoises, estimates grain params, encodes the clean picture,
  signals grain for the decoder.
- Typical `N`: ~4 animation / clean, ~8 normal live action, ~10–15 very
  grainy.
- Extra CPU cost is small next to preset 3 encode time.
- This is what `--denoise` (no value) enables today, with `N=8`.

### B. ffmpeg denoise + SVT grain without SVT denoise

```
--vfilter "hqdn3d=..."   # or atadenoise / nlmeans
--svt film-grain=N --svt film-grain-denoise=0
```

- Better control of the denoise filter.
- `nlmeans` can be **slower than the encode itself** on 1080p.
- `hqdn3d` / `atadenoise` are much cheaper, a bit coarser.
  (nlmeans stays out: over budget even in fast form on 1080p).

## Detecting grain

Implemented as `--noise` (no arguments): 3 short samples are encoded
fast, raw vs each candidate filter; filters over ~30s per sample are
out; the winner applies (`off` under 5% removable grain); the grain
model level (4/8/12) follows the measured share. Leftover ideas:
profile heuristic (film on, animation off), `bitplanenoise` metric.

Neither SVT nor libaom auto-picks a perfect `film-grain` level for you.
Community starting points above are rules of thumb.

## Size savings (speculative)

| Source | Rough video-stream saving |
|--------|---------------------------|
| Almost no grain | 0–10% (sometimes worse: syntax overhead) |
| Normal cinema grain | ~15–35% |
| Heavy grain / old film scan | ~40–70% (extreme cases higher) |
| Clean animation | usually better left off |

Numbers vary a lot with content. Netflix publicly reported large
savings on grainy titles and ~36% average bitrate cut for 1080p+ when
FGS is on across a grainy catalog mix.

## Cost (order of magnitude)

| Step | No FGS | A: SVT FGS | B: heavy nlmeans + FGS |
|------|--------|------------|-------------------------|
| Grain probe (short sample) | — | seconds | seconds–minutes |
| crf-search | baseline | +~5–20% | can be ×2–10 |
| Full encode | baseline | +~5–15% | dominated by filter |
| Playback | normal | slightly more decode CPU for grain synth | same |

## VMAF caveat (important for us)

ab-av1 picks CRF with VMAF. Synthesized grain is **not** the same
pixels as the source grain. VMAF often sees the denoised/synth path
as worse, so:

- the same `--vmaf` target may force a lower CRF / larger file, or
- subjective quality may look fine while VMAF looks “bad”.

Film-grain synthesis is partly a **subjective** tool. Blind VMAF 94/95
may need retuning when `--denoise` is on. Some people lower the VMAF
target a bit and rely on grain to hide artifacts.

Also: with `film-grain-denoise=0` and a very high VMAF target, the
encoder may start coding real grain *and* still signal synth grain
(“grain on grain”). With denoise=1 that risk is lower.

## Filter benchmark (measured 2026-09-14, 2x10s 1080p clips)

removal = 1 - size(filtered)/size(raw) at fixed fast encode
(preset 10, CRF 32); budget% = 100 * filter time / preset-3
encode time (target <= 50); SSIM/VMAF = filtered vs grainy source
(filter-only change; VMAF is grain-blind, so high VMAF + high
removal is the ideal combo).

| filter | removal nosferatu/tomahawk | VMAF | budget% |
|---|---|---|---|
| removegrain mode 2 | 12.4 / 2.9 | 99.3 / 99.7 | 2 / 4 |
| fftdnoiz default | 10.5 / 6.9 | 98.7 / 98.8 | 16 / 20 |
| vaguedenoiser default | 7.2 / 5.1 | n/a | 34 / 53 |
| nlmeans mid (r=5) | 7.7 / 6.6 | n/a | 79 / 122 |
| hqdn3d=2:2:4:4 | 6.6 / 1.9 | 98.7 / 98.3 | 6 / 10 |
| nlmeans fast (r=3) | 1.6 / 2.9 | n/a | 32 / 46 |
| atadenoise default | 2.5 / 0.4 | n/a | 3 / 3 |
| bilateral default | 0.0 (no-op) | n/a | 6 / 8 |
| nlmeans default, owdenoise, dctdnoiz | no data | n/a | 180-392 |

Excluded by budget: nlmeans default, owdenoise (both), dctdnoiz
(also negative removal: adds bits). `bilateral` defaults do
nothing. Third-party (VapourSynth BM3D/KNLMeans/SMDegrain/DFTTest,
Neat Video): best quality on paper, but need GPU or plugin stacks
that are not installed here, CPU times far over budget, and
licenses (commercial/GPL-mix) do not fit a plain ffmpeg call.
Verdict: stay with ffmpeg-native filters; `removegrain` is the
next `--noise` mode candidate (strongest removal, fastest,
least perceived change).

## Playback support

Grain is restored only if the decoder applies AV1 film-grain. Modern
`dav1d` / common players usually do. Unusual setups that strip grain
will show the cleaner (denoised) picture.

## What we implemented so far

- `--noise` is a bare flag: it races `removegrain`,
  `fftdnoiz`/`fftdnoiz-strong`, `hqdn3d`/`hqdn3d-strong`,
  `atadenoise` on 3 short samples (fast preset-10 encode, raw vs
  filtered); filters over ~30s per sample are out. The winner is
  used (`off` under 5% removable grain); the grain level follows
  the share (4/8/12). The winning filter goes to `--vfilter` plus
  `film-grain=N`, `film-grain-denoise=0` (real denoise; ab-av1
  scores VMAF against the filtered reference, so scores stay
  honest). Shares, times and pick are printed; the resolved
  `algo:N` mode keys the main cache (`.noise{algo}{level}`), and
  the verdict itself is remembered in `.noise.json`, so a repeat
  run skips the race.
- Approach A (SVT-internal denoise) retired: not tunable, and checks
  kept finding visible grain left behind. There are no per-filter
  CLI modes: the race picks the filter.
- `--crop` chains as `crop=w:h:x:y,<denoise filter>` in one
  `--vfilter`: crop area (rows/columns dark in ~90% of 24 gray
  probe frames across 3 windows, else off) has its own cache suffix
  and does not disturb the denoise verdict. cropdetect is not used:
  its limit is not scaled to 10-bit sources, so bar noise counts as
  content and it latches onto the widest flash frame.


## Open questions

1. Should the film profile default `--noise` on, and the animation profile off?
2. Grain level calibration: auto picks 4/8/12 from the measured share; needs viewing data.
3. ~~Add cheap ffmpeg modes~~ done as race candidates.
4. How to adjust `--vmaf` policy when denoise is on?
5. Do we ever want `film-grain-denoise=0` with external denoise only?
