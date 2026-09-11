# mkv — smart movie compression

Small tool to compress movies to **AV1 video + Opus audio** with fixed
quality and no wasted bytes. Run one command, get a smaller file with
the quality you asked for.

## Files

- `mkvf` — compress a **film** (VMAF ≥ 94, audio SI-SDR ≥ 18 dB)
- `mkv` — compress **animation** (VMAF ≥ 90, audio SI-SDR ≥ 15 dB)
- `mkv_encode.py` — the main script (both wrappers call it)

## Needs

- `ab-av1`
- `ffmpeg` / `ffprobe` (with `libopus`, `asisdr`, `astats` filters)
- `python3`

## Use

```sh
mkvf film.mkv      # compress a film
mkv cartoon.mkv    # compress animation
```

The script keeps the original as `film.mkv.bak` and puts the small file
in its place. If a `.bak` file is already there, it asks first.

Custom targets:

```sh
mkv_encode.py --min-vmaf 95 --min-sisdr 20 film.mkv
```

## How it works

Video: `ab-av1 crf-search` finds the highest CRF that still hits the
VMAF target, then `ab-av1 encode` compresses the file.

Audio: for each track, a 60-second sample from the middle of the film
is encoded to Opus at low bitrates first. The lowest bitrate that hits
the SI-SDR target wins — the same idea as CRF search, but for sound.

To save every byte:

- the search never goes above a lossy source bitrate
  (a re-encode cannot sound better than its source);
- Opus that is already good enough is copied, not re-encoded;
- silent channels (LFE pauses and such) do not count in the score;
- 6-channel tracks with the `5.1(side)` layout are fixed to plain `5.1`.

Search results (CRF + audio bitrates) are cached in `/tmp`, keyed by
file name, size, and quality targets.

## Output

The script stays quiet. You see the normal `ab-av1` output, plus one
short line per audio track and one last line with old/new file sizes.
