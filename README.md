# mkv — smart movie compression

Small tool to compress movies to **AV1 video + Opus audio** with fixed
quality and no wasted bytes. Run one command, get a smaller file with
the quality you asked for.

## Setup

Wrappers live in `~/bin` and call the script in this repo:

- `mkvf` — film defaults: `--vmaf 94 --sdr 72`
- `mkv` — animation defaults: `--vmaf 90 --sdr 60`
- `mkv_encode.py` — the main script

## Needs

- `ab-av1`
- `ffmpeg` / `ffprobe` (with `libopus`, `asisdr`, `astats` filters)
- `python3`

## Use

```sh
mkvf film.mkv      # compress a film
mkv cartoon.mkv    # compress animation
```

Custom subjective targets:

```sh
mkv_encode.py --vmaf 95 --sdr 90 film.mkv
```

- `--vmaf` — target video VMAF (same scale as ab-av1)
- `--sdr` — subjective audio quality **0..100**, mapped linearly to
  SI-SDR (`--sdr 100` → 25 dB). The search then picks the lowest Opus
  bitrate that hits that SI-SDR on a mid-film sample.

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

Ctrl+C (or kill) removes temp encode files and search scratch dirs so
the disk looks as it did before the run. Leftovers from a hard crash
or power loss are removed automatically on the next start.

Run tests with:

```sh
python3 -m pytest tests/
```

## How it works

1. Audio: for each track, a 60-second sample from the middle of the film
   is encoded to Opus at low bitrates first. The lowest bitrate that
   hits the SI-SDR target wins.
2. Video: `ab-av1 crf-search` finds the highest CRF that still hits the
   VMAF target, then `ab-av1 encode` compresses the file.

To save every byte:

- the search never goes above a lossy source bitrate
  (a re-encode cannot sound better than its source);
- Opus that is already good enough is copied, not re-encoded;
- silent channels (LFE pauses and such) do not count in the score;
- 6-channel tracks with the `5.1(side)` layout are fixed to plain `5.1`.

Search results (CRF + audio bitrates) are cached in `/tmp`, keyed by
file name, size, and quality targets.

## Output

Only two things on screen:

1. one updating audio-search line (same `\r` style as ab-av1 encode);
2. the normal `ab-av1` progress bars for crf-search and encode.

`crf-search` is run with `--stdout-format json` so the script can read the
chosen CRF from stdout while **stderr stays on the real terminal** — that
is what keeps ab-av1's progress bar instead of INFO log spam.
