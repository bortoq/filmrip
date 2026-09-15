# mkv — smart movie compression

Small tool to compress movies to **AV1 video + Opus audio** with fixed
quality and no wasted bytes. Run one command, get a smaller file with
the quality you asked for.

## Setup

```sh
git clone https://github.com/bortoq/filmrip.git
cd filmrip
```

## Needs

- `ab-av1` — video search and encode.
  Install: https://github.com/alexheretic/ab-av1/releases
  (or `cargo install ab-av1`). Tested: 0.11.7.
  Check: `ab-av1 --version`.
- `ffmpeg` + `ffprobe` — audio search, filters, muxing.
  Install: https://ffmpeg.org/download.html
  (static builds include everything below).
  Needs the `libopus` and `libsvtav1` encoders plus the `asisdr`
  and `astats` filters. Tested: 8.1.
  Check: `ffmpeg -hide_banner -h encoder=libopus`,
  `ffmpeg -hide_banner -h filter=asisdr`.
- `python3` (standard library only, no packages).
  Check: `python3 --version`.

## Use

```sh
python3 mkv_encode.py film.mkv
python3 mkv_encode.py --vmaf 90 --sdr 60 cartoon.mkv   # animation
```

Custom subjective targets:

```sh
mkv_encode.py --vmaf 95 --sdr 90 film.mkv
mkv_encode.py --noise film.mkv   # race filters, use winner
mkv_encode.py --crop film.mkv    # cut black bars first
                            # no flag = no denoise (default)
```

- `--vmaf` — target video VMAF (same scale as ab-av1)
- `--sdr` — subjective audio quality **0..100**, mapped linearly to
  SI-SDR (`--sdr 100` → 25 dB). The search probes 30 short places
  to rank them by difficulty, bisects the bitrate ladder on the 3
  hardest, and takes the median need.
- `--noise` — grain removal. The program probes 3 short samples
  with a fast encode (source vs each filter) and races `removegrain`,
  `fftdnoiz` (+strong sigma), `hqdn3d` (+strong), `atadenoise`;
  filters slower than ~30 seconds per sample are out. The winner is
  used (`off` under 5% removable grain); the grain model level (4/8/12)
  follows the measured share. Every mode stores an AV1 grain model,
  so the decoder paints grain back.  Leave it off for animation.
- `--crop` — probe gray frames in 3 windows and cut rows/columns
  dark in nearly every frame (`- crop 1920:800:0:140`, `- crop off`
  when unclear). One bright flash cannot widen the area.
  Same crop goes to `crf-search` and `encode`, with its own cache
  key; chains with `--noise` into one filter.

Approximate Opus bitrate **per channel** for film-like audio
(stereo total ≈ 2×; 5.1 total ≈ 6×):

| `--sdr` | SI-SDR | ≈ kbps/ch |
|--------:|-------:|----------:|
|      32 |    8.0 |        24 |
|      60 |   15.0 |        28 |
|      65 |   16.2 |        32 |
|      68 |   17.0 |        36 |
|      72 |   18.0 |        40 |
|      77 |   19.3 |        48 |
|      82 |   20.5 |        56 |
|      87 |   21.8 |        64 |
|      95 |   23.7 |        80 |
|     100 |   25.0 |        96 |

`--sdr 100` is where Opus is practically transparent for this content.
Real bitrates vary with the track; the table is a planning guide.

The script keeps the original as `film.mkv.bak` and puts the small file
in its place. If a `.bak` file is already there, it asks first.
If the power fails in the middle of the file swap, the original is
still safe in `film.mkv.bak` — just rename it back to `film.mkv`.

Ctrl+C (or kill) stops ab-av1/ffmpeg and removes every temp this run
created: the `_tmp_encode` output, `/tmp/mkv_*` scratch dirs, and the
private ab-av1 `--temp-dir` (so no `.ab-av1-*` folders stay next to the
movie). Leftovers from a hard crash or power loss — including stray
`.ab-av1-*` dirs beside the file or in the current directory — are
removed automatically on the next start.

Run tests with:

```sh
python3 -m pytest tests/
```

## How it works

1. Audio: for each track, 30 short places (5 seconds each) are
   probed at the middle bitrate to rank them by difficulty, then the
   bitrate ladder is bisected on the 3 hardest places (10-second
   samples). The median need wins: typical hard content keeps
   the target at the lowest bitrate.
2. Video: `ab-av1 crf-search` finds the highest CRF that still hits the
   VMAF target, then `ab-av1 encode` compresses the file.

To save every byte:

- the search never goes above a lossy source bitrate
  (a re-encode cannot sound better than its source);
- Opus that is already good enough is copied, not re-encoded;
- silent channels (LFE pauses and such) do not count in the score;
- 6-channel tracks with the `5.1(side)` layout are fixed to plain `5.1`.

## Cache

Everything slow is remembered in `/tmp`, so a repeat run only
encodes. Identity is the file name plus size (targets are part of
the key where they matter):

- `NAME.SIZEb.vmafV.sdrS[.noiseALGOlvl][.cropWxH+X+Y].json` — the
  CRF plus the per-track audio plan `(bitrate, method, SI-SDR,
  pct)`. Denoise and crop runs each get their own key (crop offsets
  included): a CRF found for one filter or picture area is never
  reused for another.
- `NAME.SIZEb.noise.json` — the `--noise` race verdict (winning
  filter and grain share), keyed by file alone: it depends only on
  the source, not on targets.
- `NAME.SIZEb.crop.json` — the `--crop` probe verdict (bar area or
  none), keyed by file alone for the same reason.
- Legacy `NAME.SIZEb.vmafV.sdrS.json` and `NAME.SIZEb.vmafV.crf`
  from older runs still load (plain runs only: denoise/crop runs
  never touch them, so one mode cannot poison another).
- A genuine miss ("no CRF fits in 80% of the source") is remembered;
  a crashed search is not — the next run searches again.
- Leftovers of a killed run (`*_tmp_encode.mkv`,
  `.tmp.ab-av1-encoding.*`, `/tmp/mkv_*`) are removed on interrupt
  and on the next start.

Caches are plain JSON and safe to delete; the worst case is one
slower run. Replacing a file while keeping its name and byte size
fools the identity check — delete its `/tmp` entries then.

Exit codes: 0 means the file was compressed (or the run only
re-checked a finished job); 1 means nothing was written — bad
arguments, a declined `.bak` overwrite, a failed search/encode, or
"nothing to compress" (every stream already fits the targets, the
file is left unchanged). Ctrl+C stops the run and cleans up.

Memory: a preset-3 1080p encode needs several gigabytes of free RAM.
If ffmpeg dies with no message, look for an out-of-memory kill
(`journalctl --since "24 hours ago" | grep -i "out of memory"`)
and retry with heavy jobs stopped.

If no CRF reaches the VMAF target within 80% of the source size
(typical for efficient x265 sources at high targets), the video
stream is kept as is and only the audio is compressed. The message
shows the best sample VMAF, so you can lower `--vmaf` and try again.
A crashed search is never remembered: the next run searches again.

## Output

Every progress bar shares one template (the ab-av1 shape):
spinner, clock, name, wide bar, and `(metric, eta 2m)` on the right.
No brackets, no step counts. One bar at a time: grain probe, then
one audio bar per track, then the ab-av1 bars for crf-search and
encode. Every bar appears at once, before its slow work starts.

Result lines share one shape too, each tagged by stream:

- `- sound 1: vbr 48 SI-SDR 32 (20%)`, `- sound 2: copy (100%)`
  (one track is not numbered: `- sound: vbr 48 SI-SDR 32 (20%)`);
- `- hqdn3d-strong, 5:5:8:8 (34%)`, `- off`;
- `- crop 1920:800:0:140`, `- crop off`.

Percents everywhere mean 100 * new size / old size, same as the
ab-av1 percent. The screen is left clean: cursor back, colors reset,
no half-drawn lines.

`crf-search` is run with `--stdout-format json` so the script can read the
chosen CRF from stdout while **stderr stays on the real terminal** — that
is what keeps ab-av1's progress bar instead of INFO log spam.
