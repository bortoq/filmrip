# Roadmap: mkv smart compression

Contract for developing this program. Simple English.

## Output uniformity

- One bar template everywhere, the ab-av1 shape:
  spinner, clock, name, wide bar, `(metric, eta 2m)`.
- No brackets around bars. No step counts in tails; progress is
  always `eta`. Plain output (logs, pipes) mirrors it in ASCII.
- Result lines share one shape and always name the stream:
  `- sound 1: vbr 48 SI-SDR 32 (20%)`, `- sound 2: copy (100%)`,
  `- hqdn3d-strong, 5:5:8:8 (34%)`, `- off`,
  `- crop 1920:800:0:140`, `- crop off`.
- Commas separate name, params, and eta inside parens:
  `(hqdn3d-strong, 5:5:8:8, eta 1m)`.
- Percents everywhere mean 100 * new size / old size, same as the
  ab-av1 percent. Never a difference ratio.
- One bar at a time, each shown at once before its slow work.
- The screen is left clean: cursor back, colors reset, no
  half-drawn lines (tty only, never pipes).

## Search rules

- Same params go to crf-search and encode, so the found setting
  fits what is encoded.
- Cache keys cover every choice: quality targets, filter and grain
  level, crop area. A crashed search is never remembered.
- Never upscale: copy instead of re-encoding at or above the source
  size, and when savings are tiny.
- Audio: rank 30 short places, bisect the ladder on the 3 hardest,
  take the median need; exact (non-ladder) bitrates allowed after
  one verify encode.
- Noise: `--noise` takes no arguments; the program races filters on
  short probes and uses the winner. Filters over ~30s per sample
  are out. SVT-internal denoise is retired.
- Crop: consensus of 3 samples (2 of 3 must agree); reject areas
  outside the frame or under 40% of it; chains with denoise.

## Runtime rules

- Films are read-only; all scratch files live in system temp.
- Shared temp sweeps only touch entries older than one hour, so a
  live parallel run is never harmed.
- No hidden side effects; every system call result is checked;
  failures degrade to copying the stream, never to a crash.
