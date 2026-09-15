#!/usr/bin/env python3
"""Smart movie compression to AV1 (video) + Opus (audio) at minimal bitrate.

    python3 mkv_encode.py film.mkv                              # defaults
    python3 mkv_encode.py --vmaf 90 --sdr 60 cartoon.mkv         # animation
    python3 mkv_encode.py --vmaf 95 --sdr 90 --noise --crop film.mkv

How it works:
    audio: for each track, 30 short places (5 seconds each) are
           ranked by difficulty at the middle bitrate, the ladder is
           bisected on the 3 hardest places (10-second samples), the
           exact need is secant-refined and verified, and the maximum
           need wins (--sdr 0..100 maps linearly to 0..25 dB SI-SDR;
           see README table);
    video: ab-av1 crf-search finds the highest CRF with VMAF >= --vmaf,
           then ab-av1 encode compresses the whole file;
    options: --noise races denoise filters and keeps an AV1 grain
           model; --crop cuts black bars; both verdicts are cached.

    A stream that cannot hit its target with a smaller size is copied
    as is. When nothing can be compressed (the video and every audio
    track end up copied), the script says so and exits without
    touching the file (exit 1, see README).

Screen output is kept minimal: one updating bar per slow step
(ab-av1 shape) that becomes a result line such as
`- sound 1: vbr 48 SI-SDR 32 (20%)`, then the ab-av1 bars.
Slow searches (CRF, audio plan, noise/crop verdicts) are cached in
/tmp (see README ## Cache).
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
import threading
import time

PRESET = 3
SAMPLE_SECS = 60  # cap for the source-bitrate guess window, in seconds
CACHE_DIR = "/tmp"

# --noise NAME: grain handling (doc/research.md).
# An ffmpeg filter removes the grain before encode, SVT only stores
# the grain model (film-grain-denoise=0) the decoder paints back.
# Available filters: hqdn3d, atadenoise, removegrain, fftdnoiz.
# (SVT-internal denoise was removed: not tunable and too weak --
# measured checks kept finding visible grain left behind.)
# The same params go to crf-search and to the final encode, so the
# found CRF fits what is encoded. Grain levels: 12 light, 24 medium,
# 40 heavy (restoration parity, measured live). Synthesized grain
# is not the same pixels as the source grain, so VMAF (measured
# against the grainy source) scores the denoised path lower: the same
# --vmaf may need a lower CRF / larger file; drop --vmaf by ~1-2 when
# --noise is on if size matters.
SVT_FILM_GRAIN_DEFAULT = 8
SVT_FILM_GRAIN_MIN = 1
SVT_FILM_GRAIN_MAX = 50
# hqdn3d strengths are luma_spatial:chroma_spatial:luma_tmp:chroma_tmp
# (ffmpeg defaults are all 0, i.e. bare "hqdn3d" does nothing).
HQDN3D_DEFAULT = "hqdn3d=2:2:4:4"
# atadenoise defaults (0.02/0.04 thresholds, 9 frames) are a mild
# adaptive temporal denoise, so bare "atadenoise" is a sane default.
ATADENOISE_DEFAULT = "atadenoise"
DENOISE_ALGOS = ("hqdn3d", "hqdn3d-strong", "atadenoise",
                 "removegrain", "fftdnoiz", "fftdnoiz-strong")
# Competition set for bare --noise: the two strongest cheap filters
# (measured: removegrain removes most per second, fftdnoiz second).
FILTER_CANDIDATES = {"removegrain": "removegrain=m0=2:m1=2:m2=2",
                     "fftdnoiz": "fftdnoiz",
                     "fftdnoiz-strong": "fftdnoiz=sigma=3",
                     "hqdn3d": "hqdn3d=2:2:4:4",
                     "hqdn3d-strong": "hqdn3d=5:5:8:8",
                     "atadenoise": "atadenoise"}


def parse_denoise(value):
    """Normalize a --noise value to a mode string or None.

    --noise takes no arguments: None/False (off) or True/"auto"
    (probe the source and pick). Any other value raises ValueError;
    resolved "algo[:N]" modes are built internally, never typed.
    """
    if value is None or value is False:
        return None
    if value is True:
        return "auto"
    if not isinstance(value, str):
        raise ValueError(f"bad denoise mode: {value!r}")
    if value.strip().lower() == "auto":
        return "auto"
    raise ValueError(f"bad denoise mode: {value!r}")


def denoise_algo(mode):
    """Algorithm name for a parsed mode; None when off."""
    if mode is None or not isinstance(mode, str):
        return None
    m = mode.strip().lower()
    if m in ("off", "none", "no", "0", "auto"):
        return None
    for algo in DENOISE_ALGOS:
        if m == algo or m.startswith(algo + ":"):
            return algo
    return None


def denoise_grain_level(mode):
    """Grain level int for a parsed denoise mode; None when off."""
    if denoise_algo(mode) is None:
        return None
    m = mode.strip().lower()
    for algo in DENOISE_ALGOS:
        if m == algo:
            return SVT_FILM_GRAIN_DEFAULT
    try:
        n = int(m.split(":", 1)[1].strip())
    except (ValueError, TypeError, IndexError):
        return None
    if SVT_FILM_GRAIN_MIN <= n <= SVT_FILM_GRAIN_MAX:
        return n
    return None


def svt_args_for_denoise(mode):
    """SVT key=value args for a parsed denoise mode (no --svt prefix).

    Returns [] when off. Every mode denoises with an ffmpeg filter,
    so SVT only stores the grain model (film-grain-denoise=0).
    Raises ValueError on unknown modes.
    """
    algo = denoise_algo(mode)
    if algo is None:
        if mode is None or (isinstance(mode, str)
                            and mode.strip().lower()
                            in ("off", "none", "no", "0")):
            return []
        raise ValueError(f"bad denoise mode: {mode!r}")
    level = denoise_grain_level(mode)
    if level is None:
        raise ValueError(f"bad denoise mode: {mode!r}")
    return [f"film-grain={level}", "film-grain-denoise=0"]


def vfilter_for_denoise(mode):
    """ffmpeg --vfilter string for a parsed mode; None when unneeded.

    Only "hqdn3d"/"atadenoise" filter before encode (ab-av1 applies
    the same filter to the VMAF reference, so scores stay honest).
    Raises ValueError on unknown modes.
    """
    algo = denoise_algo(mode)
    if algo is None:
        if mode is None or (isinstance(mode, str)
                            and mode.strip().lower()
                            in ("off", "none", "no", "0")):
            return None
        raise ValueError(f"bad denoise mode: {mode!r}")
    if algo == "hqdn3d":
        return HQDN3D_DEFAULT
    if algo == "atadenoise":
        return ATADENOISE_DEFAULT
    if algo in FILTER_CANDIDATES:
        return FILTER_CANDIDATES[algo]
    return None

# Policy: every stream is either compressed to the target quality or
# copied as is. crf-search fails with "Failed to find a suitable crf"
# when the VMAF target needs a file larger than the source (its
# --max-encoded-percent cap is 80% of the input; grainy or already
# well-encoded sources hit this) -- then the video stream is copied,
# not re-encoded. The same rule keeps audio tracks that cannot hit the
# SI-SDR target as is.

# --sdr 100 maps to this SI-SDR (dB). Around here Opus is effectively
# transparent for film audio (~96 kbps per channel).
SISDR_AT_100 = 25.0

# Audio pick, ab-av1 style: rank many short places, then bisect the
# hardest ones. Stage 1 probes RANK_POSITIONS places of RANK_SECS
# seconds at the ladder middle to order them by difficulty (lower
# SI-SDR first; failed probes count as hardest). Stage 2 bisects the
# bitrate ladder on the REFINE_TOP hardest places (REFINE_SECS samples
# centered on the same spots): bisection (at most BISECT_PROBES
# probes), a walk down to the true first hitting rung (WALK_PROBES),
# then secant steps on the measured bracket (SECANT_PROBES). Every
# returned bitrate is verified on its place, never a raw ladder rung.
# Scores rise smoothly with bitrate (measured: 0.2% dips), so
# bisection and the secant line are safe.
RANK_POSITIONS = 30
RANK_SECS = 5.0
REFINE_TOP = 6
REFINE_SECS = 10.0
BISECT_PROBES = 6
WALK_PROBES = 3
SECANT_PROBES = 3
REFINE_PROBES = BISECT_PROBES + WALK_PROBES + SECANT_PROBES
# ...a pick must save a real chunk of the source track, otherwise the
# source is copied (no pointless re-encode of near-source sizes).
SAT_MIN_SAVING = 0.75

# Candidate total Opus bitrates (per track), in kbit/s.
BITRATE_LADDER = [16, 24, 32, 40, 48, 56, 64, 72, 80, 96, 112, 128,
                  160, 192, 224, 256, 320, 384, 448, 512]

LOSSLESS_CODECS = {"flac", "alac", "mlp", "truehd",
                   "pcm_s16le", "pcm_s24le", "pcm_s32le",
                   "pcm_f32le", "pcm_f64le", "pcm_u8", "pcm_s16be",
                   "pcm_s24be", "pcm_s32be"}

# Temp-dir prefixes created by this script (cleaned on start / exit).
_TMP_PREFIXES = ("mkv_audio_", "mkv_abrest_", "mkv_abav1_",
                 "mkv_grain_")
# Shared sweeps only touch entries older than this: a fresh entry is
# a live parallel run, not a leftover from a killed one.
_STALE_SECS = 3600
# ab-av1 sample dirs (created in cwd / next to the input unless --temp-dir).
_ABAV1_DIR_PREFIX = ".ab-av1-"
# ab-av1 output temp file next to the movie on encode, e.g.
# .tmp.ab-av1-encoding.film_tmp_encode.mkv (left behind on SIGKILL).
_ABAV1_TMP_PREFIX = ".tmp.ab-av1-encoding."

# Paths and child processes to drop on abort / exit.
_CLEANUP_FILES = set()
_CLEANUP_DIRS = set()
_CHILD_PROCS = []
_CLEANING = False
# Dedicated --temp-dir for ab-av1 crf-search (this run).
_ABAV1_TEMP = None


DEBUG = False

# True while the last stderr write left a half-drawn \r line.
# The exit path uses it to finish the line before the shell prompt.
_NEED_NL = False


def dbg(msg):
    """--debug narration: plain-language step log (no fancy bars)."""
    if DEBUG:
        eprint(msg)


def eprint(msg):
    global _NEED_NL
    print(msg, file=sys.stderr)
    _NEED_NL = False


def sdr_to_sisdr(sdr_percent):
    """Map subjective --sdr (0..100) to a SI-SDR target in dB."""
    return float(sdr_percent) / 100.0 * SISDR_AT_100


def sisdr_to_sdr(sisdr_db):
    """Map a SI-SDR score (dB) back to the subjective --sdr scale."""
    return float(sisdr_db) / SISDR_AT_100 * 100.0


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
    """Terminate ab-av1 / ffmpeg process groups, then force-kill if needed."""
    for p in list(_CHILD_PROCS):
        pid = getattr(p, "pid", None)
        if not pid:
            continue
        # Prefer killing the whole session (ab-av1 + ffmpeg children).
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                p.terminate()
            except Exception:
                pass
        try:
            p.wait(timeout=3)
            continue
        except Exception:
            pass
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                p.kill()
            except Exception:
                pass
        try:
            p.wait(timeout=2)
        except Exception:
            pass
    _CHILD_PROCS.clear()


def cleanup_generated(quiet=True):
    """Remove temp files/dirs produced by this run; kill child processes."""
    global _CLEANING, _ABAV1_TEMP
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
        _ABAV1_TEMP = None
    finally:
        _CLEANING = False
    _restore_terminal()


def _restore_terminal():
    """Bring the terminal back: show cursor, reset colors, end line.

    ab-av1 hides the cursor while its bars run; if it dies by signal
    (or we kill it), nothing restores it. Only touches a real tty,
    never pipes or files. Never raises.
    """
    global _NEED_NL
    try:
        if not sys.stderr.isatty():
            _NEED_NL = False
            return
    except Exception:
        return
    try:
        out = "\x1b[?25h\x1b[0m"
        if _NEED_NL:
            out += "\n"
        sys.stderr.write(out)
        sys.stderr.flush()
    except Exception:
        pass
    _NEED_NL = False


def _is_abav1_dir_name(name):
    return isinstance(name, str) and name.startswith(_ABAV1_DIR_PREFIX)


def _is_stale(path):
    """True when the entry is old enough to be a leftover. Never raises."""
    try:
        return time.time() - os.stat(path).st_mtime > _STALE_SECS
    except (ValueError, TypeError, OSError):
        return True


def _rm_abav1_dirs_in(parent):
    """Remove every .ab-av1-* directory under parent."""
    if not parent:
        return
    try:
        names = os.listdir(parent)
    except OSError:
        return
    for name in names:
        if not _is_abav1_dir_name(name):
            continue
        path = os.path.join(parent, name)
        if not _is_stale(path):
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _rm_abav1_tmp_files_in(parent):
    """Remove .tmp.ab-av1-encoding.* files under parent."""
    if not parent:
        return
    try:
        names = os.listdir(parent)
    except OSError:
        return
    for name in names:
        if not isinstance(name, str):
            continue
        if not name.startswith(_ABAV1_TMP_PREFIX):
            continue
        path = os.path.join(parent, name)
        if not _is_stale(path):
            continue
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


def _rm_prefixed_in_tmpdir():
    """Remove /tmp/mkv_audio_*, mkv_abrest_*, mkv_abav1_*, mkv_grain_."""

    tmp_root = tempfile.gettempdir()
    try:
        names = os.listdir(tmp_root)
    except OSError:
        return
    for name in names:
        if not any(name.startswith(p) for p in _TMP_PREFIXES):
            continue
        path = os.path.join(tmp_root, name)
        if not _is_stale(path):
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


def cleanup_stale_temps(input_path=None):
    """Drop leftovers from a previous killed / power-loss run.

    Cleans:
      - <input>_tmp_encode<ext> next to the movie;
      - .tmp.ab-av1-encoding.* output temps next to the movie and in
        cwd (left behind when ffmpeg is killed mid-encode);
      - .ab-av1-* sample dirs next to the movie and in cwd
        (ab-av1 default location when --temp-dir is not set);
      - /tmp/mkv_audio_*, mkv_abrest_*, mkv_abav1_*, mkv_grain_
        from this tool.
    """
    if input_path:
        abs_input = os.path.realpath(input_path)
        base, ext = os.path.splitext(abs_input)
        stale = base + "_tmp_encode" + ext
        # Age-guarded like every other path: a parallel run of the
        # same file owns a fresh output, never touch it.
        if os.path.isfile(stale) and _is_stale(stale):
            try:
                os.remove(stale)
            except OSError as e:
                eprint(f"Cannot remove stale temp file {stale}: {e}")
                return False
        _rm_abav1_dirs_in(os.path.dirname(abs_input))
        _rm_abav1_tmp_files_in(os.path.dirname(abs_input))
    try:
        _rm_abav1_dirs_in(os.getcwd())
        _rm_abav1_tmp_files_in(os.getcwd())
    except OSError:
        pass
    _rm_prefixed_in_tmpdir()
    return True


def ensure_abav1_temp():
    """Create (once per run) a private --temp-dir for ab-av1 samples."""
    global _ABAV1_TEMP
    if _ABAV1_TEMP and os.path.isdir(_ABAV1_TEMP):
        return _ABAV1_TEMP
    _ABAV1_TEMP = tempfile.mkdtemp(prefix="mkv_abav1_")
    _register_dir(_ABAV1_TEMP)
    return _ABAV1_TEMP


def _spawn(cmd, **kwargs):
    """Popen in a new session so we can kill the whole process group."""
    kwargs.setdefault("start_new_session", True)
    return subprocess.Popen(cmd, **kwargs)


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


def video_size(path):
    """Source picture size (w, h); (None, None) when unknown."""
    if not path or not os.path.isfile(path):
        return None, None
    r = run_quiet(["ffprobe", "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=width,height",
                   "-of", "json", path])
    if r.returncode != 0:
        return None, None
    try:
        streams = json.loads(r.stdout or "{}").get("streams", [])
        w = int(streams[0].get("width") or 0)
        h = int(streams[0].get("height") or 0)
    except (ValueError, TypeError, IndexError, KeyError):
        return None, None
    if w <= 0 or h <= 0:
        return None, None
    return w, h


CROP_SECS = 30.0
CROP_FRACS = (0.15, 0.5, 0.85)
CROP_LIMIT = 24
CROP_TOP_AGREE = 0.9
CROP_SIDE_AGREE = 0.99
CROP_MIN_AREA = 0.4
CROP_MIN_BOXES = 10


def parse_bbox_line(line):
    """(x1, x2, y1, y2) inclusive box from one bbox log line.

    Lines without a box (fully dark frames) give None.
    Guards types and values; never raises.
    """
    if not line or not isinstance(line, str):
        return None
    try:
        m = re.search(r"x1:(\d+)\s+x2:(\d+)\s+y1:(\d+)\s+y2:(\d+)",
                      line)
    except (ValueError, TypeError):
        return None
    if not m:
        return None
    try:
        box = tuple(int(m.group(i)) for i in (1, 2, 3, 4))
    except (ValueError, TypeError):
        return None
    x1, x2, y1, y2 = box
    if x1 < 0 or y1 < 0 or x2 < x1 or y2 < y1:
        return None
    return box


def pick_crop_box(x1s, x2s, y1s, y2s, src_w, src_h):
    """Bar area "w:h:x:y" from per-frame bbox edges; None when unclear.

    Top/bottom edges hold in CROP_TOP_AGREE of the frames (letterbox
    bars are structural); side edges need CROP_SIDE_AGREE (dark scene
    corners must never pass for pillarbox bars). Edges snap outward
    to even yuv420 coordinates, so no content pixel is ever cut.
    A full-frame box means no bars. Never raises.
    """
    try:
        sw = int(src_w)
        sh = int(src_h)
        if sw <= 0 or sh <= 0:
            return None
        xs1 = sorted(int(v) for v in (x1s or []))
        xs2 = sorted(int(v) for v in (x2s or []))
        ys1 = sorted(int(v) for v in (y1s or []))
        ys2 = sorted(int(v) for v in (y2s or []))
    except (ValueError, TypeError):
        return None
    n = len(xs1)
    if (n < CROP_MIN_BOXES or len(xs2) != n or len(ys1) != n
            or len(ys2) != n):
        return None
    try:
        top_need = math.ceil(CROP_TOP_AGREE * n)
        side_need = math.ceil(CROP_SIDE_AGREE * n)
        # largest edge still covered by the agreeing share
        y0 = ys1[n - top_need]
        x1 = xs2[side_need - 1]
        y1 = ys2[top_need - 1]
        x0 = xs1[n - side_need]
    except (ValueError, TypeError, IndexError):
        return None
    # snap outward to even yuv420 coordinates: content is never
    # cut, at most one bar row stays
    x0 -= x0 % 2
    y0 -= y0 % 2
    if x1 % 2 == 0 and x1 + 1 < sw:
        x1 += 1
    if y1 % 2 == 0 and y1 + 1 < sh:
        y1 += 1
    x1 = min(x1, sw - 1)
    y1 = min(y1, sh - 1)
    w = x1 - x0 + 1
    h = y1 - y0 + 1
    if w <= 0 or h <= 0 or x0 < 0 or y0 < 0:
        return None
    if x0 + w > sw or y0 + h > sh:
        return None
    if x0 == 0 and y0 == 0 and w == sw and h == sh:
        return None
    if w * h < CROP_MIN_AREA * sw * sh:
        return None
    return f"{w}:{h}:{x0}:{y0}"


def detect_crop(path, duration, progress=None):
    """Black-bar area "w:h:x:y" from bbox edges; None when none found.

    Runs an 8-bit bbox pass over three windows across the film: every
    frame reports its exact content box, and each edge is set where
    the agreeing share of frames puts it, so one bright flash cannot
    widen the area. progress, if given, is started at once and told
    each window. Never raises, never writes near the film.
    """
    def _show(text=None, done=False, step=False):
        if progress is None:
            return
        try:
            if done:
                progress.finish()
            elif step:
                progress.tick()
            elif text is None:
                progress.start()
            else:
                progress.note(text)
        except Exception:
            pass
    if not path or not os.path.isfile(path):
        return None
    if duration is None or duration <= 0:
        return None
    src_w, src_h = video_size(path)
    if src_w is None:
        return None
    _show()
    x1s, x2s, y1s, y2s = [], [], [], []
    try:
        fracs = list(CROP_FRACS)
        for fi, frac in enumerate(fracs):
            _show(f"crop {fi + 1}/{len(fracs)}")
            win = min(float(CROP_SECS), max(5.0, duration))
            last = max(0.0, duration - win)
            try:
                start = min(last, max(0.0, float(frac) * duration
                                     - win / 2))
            except (ValueError, TypeError):
                continue
            r = run_quiet(["ffmpeg", "-y", "-v", "info",
                           "-ss", f"{start:.1f}", "-i", path,
                           "-t", f"{win:.1f}",
                           "-vf", f"format=gray,bbox=min_val={CROP_LIMIT}",
                           "-an", "-f", "null", "-"])
            _show(step=True)
            for line in (r.stderr or "").splitlines():
                box = parse_bbox_line(line)
                if box is None:
                    continue
                x1s.append(box[0])
                x2s.append(box[1])
                y1s.append(box[2])
                y2s.append(box[3])
    except Exception:
        return None
    finally:
        _show(done=True)
    return pick_crop_box(x1s, x2s, y1s, y2s, src_w, src_h)


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
    A fast stream-copy of a short sample (up to SAMPLE_SECS) gives
    a fair guess.
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


# Terminal output: bold clock, cyan spinner and bar fill, blue bar rest.
# Colors only on a real tty (never piped, never with NO_COLOR, never on
# dumb terminals); every painted span is reset, so the terminal cannot
# be left in a broken state by our output. Names, not raw escapes, are
# used at call sites (same order as in fb2opt).
_C_BOLD = "\033[1m"
_C_SPIN = "\033[36;1m"
_C_FILL = "\033[36m"
_C_REST = "\033[34m"
_C_RESET = "\033[0m"


def _use_color():
    """TTY colors allowed right now. Never raises."""
    try:
        if not sys.stderr.isatty():
            return False
    except Exception:
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True


def _paint(text, code):
    """Wrap one span in color + reset, or plain when colors are off."""
    if not text or not code or not _use_color():
        return text if isinstance(text, str) else ""
    try:
        return code + text + _C_RESET
    except Exception:
        return text


def size_percent(new_size, old_size):
    """Size ratio in percent: 100 * new_size / old_size.

    Single formula for every percent shown to the user. Returns None
    when either size is missing or the original is not positive.
    """
    if new_size is None or old_size is None:
        return None
    try:
        new_f = float(new_size)
        old_f = float(old_size)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(new_f) or not math.isfinite(old_f):
        return None
    if old_f <= 0 or new_f < 0:
        return None
    return int(round(100.0 * new_f / old_f))


def _strip_ansi(text):
    """Visible length helper: drop ANSI color codes."""
    try:
        return re.sub(r"\x1b\[[0-9;]*m", "", text)
    except Exception:
        return text


def _parse_progress_seconds(line):
    """Seconds from one ffmpeg -progress line; None when absent."""
    if not line or not isinstance(line, str):
        return None
    try:
        for key in ("out_time_ms=", "out_time="):
            if key in line:
                raw = line.split(key, 1)[1].strip().split()[0]
                if key.endswith("ms="):
                    return max(0.0, float(raw) / 1000000.0)
                sec = 0.0
                for part in raw.replace(",", ".").split(":"):
                    sec = sec * 60.0 + float(part)
                return max(0.0, sec)
    except (ValueError, TypeError, IndexError):
        return None
    return None


def _fmt_eta(sec):
    """Short wait estimate like ab-av1: `eta 42s`, `eta 2m`."""
    try:
        sec = float(sec)
    except (ValueError, TypeError):
        return "eta ?"
    if not math.isfinite(sec) or sec < 0:
        return "eta ?"
    if sec < 60:
        return f"eta {int(sec)}s"
    if sec < 3600:
        return f"eta {max(1, int(round(sec / 60)))}m"
    return f"eta {int(sec // 3600)}h"


def _spinner_eta(done, total, elapsed):
    """Remaining wait from elapsed time and done/total fraction."""
    try:
        done, total, elapsed = float(done), float(total), float(elapsed)
    except (ValueError, TypeError):
        return None
    if not all(math.isfinite(v) for v in (done, total, elapsed)):
        return None
    if total <= 0 or done <= 0 or elapsed < 0:
        return None
    return elapsed * (total - done) / done


def _fmt_elapsed(sec):
    """Clock like ab-av1 elapsed_precise: 00:00:12."""
    try:
        sec = max(0, int(sec))
    except (ValueError, TypeError):
        sec = 0
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class _AudioProgress:
    """Single-line progress for audio search, in the style of ab-av1.

    Same template as ab-av1 on a terminal:
    spinner + clock + name + wide bar + message, for example
    `audio a1 ... 2/13 32k`. On a plain output (tests, log file,
    NO_COLOR) the same line is drawn without colors or animation:
    `audio a1 [...] 2/13 32k`.

    The screen shows one family: first the audio bar (or two bars for
    two tracks, one after another), then the ab-av1 bars. The bar shows
    search progress as done/total, not size. Size percent
    (100 * new size / old size) appears only in the final result line,
    for example `- vbr 48 SI-SDR 32 (20%)`.
    """

    # indicatif default spinner (ab-av1 uses the same library).
    _SPINNER = tuple("⠁⠁⠉⠙⠚⠒⠂⠂⠒⠲⠴⠦⠖⠒⠐⠐⠒⠓⠋")

    def __init__(self, total_steps, aindex=None):
        self.total = max(1, int(total_steps))
        self.done = 0
        self.aindex = aindex
        self._start = time.monotonic()
        self._tick = 0
        self._stage = ""
        self._bitrate = None
        self._last_visible = 0
        self._finished = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._beat = None

    def _term_width(self):
        """Visible width of our own screen (stderr), re-read each frame.

        Order: real window size of stderr, then COLUMNS-aware fallback,
        then 80. Resizing the window is picked up on the next step
        without any setting from the user.
        """
        try:
            return max(40, os.get_terminal_size(sys.stderr.fileno()).columns)
        except Exception:
            pass
        try:
            return max(40, shutil.get_terminal_size().columns)
        except Exception:
            return 80

    def _message(self):
        parts = []
        if self._bitrate is not None:
            try:
                parts.append(f"{int(self._bitrate)}k")
            except (ValueError, TypeError):
                pass
        if self._stage:
            parts.append(self._stage)
        return " ".join(parts)

    def _eta_text(self):
        left = _spinner_eta(self.done, self.total,
                            time.monotonic() - self._start)
        if left is None:
            return "eta ?"
        return _fmt_eta(left)

    def _render(self, bitrate_k=None):
        if bitrate_k is not None:
            try:
                self._bitrate = int(bitrate_k)
            except (ValueError, TypeError):
                pass
        tag = f"a{self.aindex}" if self.aindex is not None else "a?"
        msg = self._message()
        tail = f"{msg}, {self._eta_text()}" if msg else self._eta_text()
        if not _use_color():
            width = 24
            filled = int(width * self.done / self.total)
            filled = max(0, min(width, filled))
            return f"audio {tag} {'#' * filled + '-' * (width - filled)} {tail}"
        spin = self._SPINNER[self._tick % len(self._SPINNER)]
        clock = _fmt_elapsed(time.monotonic() - self._start)
        head = (f"{_paint(spin, _C_SPIN)} "
                f"{_paint(clock, _C_BOLD)} "
                f"audio {tag} ")
        tail_txt = f" ({msg}, {self._eta_text()})" if msg else f" ({self._eta_text()})"
        # visible chars outside the bar: head + spaces + tail
        plain_head = _strip_ansi(head)
        avail = self._term_width() - len(plain_head) - len(tail_txt) - 1
        width = max(10, avail)
        filled = int(width * self.done / self.total)
        filled = max(0, min(width, filled))
        bar = (_paint("#" * filled, _C_FILL)
               + _paint("-" * (width - filled), _C_REST))
        return f"{head}{bar}{tail_txt}"

    def _draw(self):
        global _NEED_NL
        line = self._render()
        vis = len(_strip_ansi(line))
        pad = max(0, self._last_visible - vis)
        sys.stderr.write("\r" + line + (" " * pad))
        sys.stderr.flush()
        self._last_visible = vis
        _NEED_NL = True

    def _pulse(self):
        self._tick += 1
        self._draw()

    def _beat_loop(self):
        while not self._stop.wait(0.2):
            with self._lock:
                if self._finished:
                    break
                try:
                    self._pulse()
                except Exception:
                    break

    def start(self, aindex=None):
        """Show the bar at once, before any slow work begins."""
        with self._lock:
            if self._finished:
                return
            if aindex is not None:
                self.aindex = aindex
            try:
                self._draw()
            except Exception:
                pass
            if self._beat is None and _use_color():
                try:
                    self._beat = threading.Thread(
                        target=self._beat_loop, daemon=True)
                    self._beat.start()
                except Exception:
                    self._beat = None

    def stage(self, text):
        """Name the current step without moving the done counter."""
        with self._lock:
            if self._finished:
                return
            self._stage = text or ""
            self._tick += 1
            try:
                self._draw()
            except Exception:
                pass

    def update(self, aindex=None, bitrate_k=None, score=None):
        with self._lock:
            if self._finished:
                return
            self.done = min(self.done + 1, self.total)
            self._tick += 1
            if aindex is not None:
                self.aindex = aindex
            global _NEED_NL
            line = self._render(bitrate_k)
            vis = len(_strip_ansi(line))
            pad = max(0, self._last_visible - vis)
            sys.stderr.write("\r" + line + (" " * pad))
            sys.stderr.flush()
            self._last_visible = vis
            _NEED_NL = True

    def finish(self, summary=None):
        """End the line: erase it, or leave `summary` as the result."""
        if self._finished:
            return
        self._finished = True
        try:
            self._stop.set()
        except Exception:
            pass
        beat, self._beat = self._beat, None
        if beat is not None and beat is not threading.current_thread():
            try:
                beat.join(timeout=1.0)
            except Exception:
                pass
        global _NEED_NL
        with self._lock:
            if summary is None:
                if self._last_visible:
                    sys.stderr.write("\r" + (" " * self._last_visible) + "\r")
            else:
                sys.stderr.write("\r" + (" " * self._last_visible) + "\r")
                sys.stderr.write(summary + "\n")
            sys.stderr.flush()
            self._last_visible = 0
            _NEED_NL = False


class _Spinner:
    """One-line activity sign, same template as every other bar.

    `spin clock  wide bar (name eta 2m)` on a terminal,
    `#--- name eta 2m` on plain output -- the ab-av1 shape.
    With total=None there is no bar, only motion (for waits with no
    natural steps). start() draws instantly; note() changes the
    message; tick() advances one step (or jumps to done=); pulse()
    moves the frame without touching done; finish() erases the line
    or leaves a result line. A background pulse keeps the clock alive
    on a real terminal only.
    """

    _SPINNER = _AudioProgress._SPINNER
    _ASCII = tuple("|/-\\")

    def __init__(self, message="", total=None):
        self._message = message or ""
        try:
            self.total = max(1, int(total)) if total is not None else None
        except (ValueError, TypeError):
            self.total = None
        self.done = 0.0
        self._start = time.monotonic()
        self._tick = 0
        self._last_visible = 0
        self._finished = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._beat = None

    def _term_width(self):
        try:
            return max(40, os.get_terminal_size(sys.stderr.fileno()).columns)
        except Exception:
            pass
        try:
            return max(40, shutil.get_terminal_size().columns)
        except Exception:
            return 80

    def _render(self):
        eta = ""
        if self.total is not None:
            left = _spinner_eta(self.done, self.total,
                                time.monotonic() - self._start)
            if left is not None and self.done < self.total:
                eta = " " + _fmt_eta(left)
        if not _use_color():
            if self.total is None:
                frame = self._ASCII[self._tick % len(self._ASCII)]
                return f"{frame} {self._message}".rstrip()
            width = 24
            filled = int(width * min(1.0, self.done / self.total))
            filled = max(0, min(width, filled))
            tail = f"{self._message}, {eta}".strip(", ") if self._message \
                else eta
            return (f"{'#' * filled + '-' * (width - filled)} "
                    f"{tail}").rstrip()
        spin = self._SPINNER[self._tick % len(self._SPINNER)]
        clock = _fmt_elapsed(time.monotonic() - self._start)
        if self.total is None:
            head = (f"{_paint(spin, _C_SPIN)} "
                    f"{_paint(clock, _C_BOLD)} {self._message} ")
            return head.rstrip()
        head = (f"{_paint(spin, _C_SPIN)} "
                f"{_paint(clock, _C_BOLD)}  ")
        tail_txt = (f" ({self._message}, {eta})"
                      if self._message else f" ({eta})")
        plain_head = _strip_ansi(head)
        avail = self._term_width() - len(plain_head) - len(tail_txt) - 1
        width = max(10, avail)
        filled = int(width * min(1.0, self.done / self.total))
        filled = max(0, min(width, filled))
        bar = (_paint("#" * filled, _C_FILL)
               + _paint("-" * (width - filled), _C_REST))
        return f"{head}{bar}{tail_txt}"

    def _draw(self):
        global _NEED_NL
        line = self._render()
        vis = len(_strip_ansi(line))
        pad = max(0, self._last_visible - vis)
        sys.stderr.write("\r" + line + (" " * pad))
        sys.stderr.flush()
        self._last_visible = vis
        _NEED_NL = True

    def _beat_loop(self):
        while not self._stop.wait(0.2):
            with self._lock:
                if self._finished:
                    break
                try:
                    self._tick += 1
                    self._draw()
                except Exception:
                    break

    def _launch_beat(self):
        if self._beat is None and _use_color():
            try:
                self._beat = threading.Thread(
                    target=self._beat_loop, daemon=True)
                self._beat.start()
            except Exception:
                self._beat = None

    def start(self, message=None):
        """Show the sign at once, before slow work begins."""
        with self._lock:
            if self._finished:
                return
            if message is not None:
                self._message = message
            try:
                self._draw()
            except Exception:
                pass
            self._launch_beat()

    def note(self, message):
        """Change the message, keep clock and counter running."""
        with self._lock:
            if self._finished:
                return
            self._message = message or ""
            self._tick += 1
            try:
                self._draw()
            except Exception:
                pass

    def tick(self, done=None):
        """Advance one step, or jump to done=. Redraws."""
        with self._lock:
            if self._finished:
                return
            if done is not None:
                try:
                    self.done = max(0.0, float(done))
                except (ValueError, TypeError):
                    pass
            elif self.total is not None:
                self.done = min(float(self.total), self.done + 1.0)
            self._tick += 1
            try:
                self._draw()
            except Exception:
                pass

    def pulse(self):
        """Move one frame without touching done (flat waits)."""
        with self._lock:
            if self._finished:
                return
            self._tick += 1
            try:
                self._draw()
            except Exception:
                pass

    def finish(self, summary=None):
        """End the sign: erase it, or leave `summary` as the result."""
        if self._finished:
            return
        self._finished = True
        try:
            self._stop.set()
        except Exception:
            pass
        beat, self._beat = self._beat, None
        if beat is not None and beat is not threading.current_thread():
            try:
                beat.join(timeout=1.0)
            except Exception:
                pass
        global _NEED_NL
        with self._lock:
            if summary is None:
                if self._last_visible:
                    sys.stderr.write("\r" + (" " * self._last_visible) + "\r")
            else:
                sys.stderr.write("\r" + (" " * self._last_visible) + "\r")
                sys.stderr.write(summary + "\n")
            sys.stderr.flush()
            self._last_visible = 0
            _NEED_NL = False


# Auto grain pick: probe a few short samples with a fast encode,
# raw vs each candidate filter. g = median(1 - filtered/raw) estimates
# how much of the stream each filter removes. The winner is used;
# below 5% nothing is worth removing. The grain level follows g:
# light 4, normal 8, heavy 12. A filter slower than AUTO_TIME_CAP
# seconds per probe sample is out (about half the preset-3 pace).
AUTO_PROBE_SECS = 10.0
AUTO_PROBE_FRACS = (0.25, 0.5, 0.75)
AUTO_PROBE_PRESET = "10"
AUTO_PROBE_CRF = "32"
AUTO_OFF_BELOW = 0.05
AUTO_TIME_CAP = 30.0


NOISE_VERDICT_VERSION = 2


def grain_level_for(g):
    """Grain model level for a measured removal share (12/24/40).

    Calibrated live: level 12 restores ~45% of the removed grain
    energy, 25 ~70%, 35 ~85%; parity sits near 40. Levels above 40
    buy little and cost VMAF against the grainy source.
    """
    try:
        g = float(g)
    except (ValueError, TypeError):
        return SVT_FILM_GRAIN_DEFAULT
    if not math.isfinite(g):
        return SVT_FILM_GRAIN_DEFAULT
    if g < 0.10:
        return 12
    if g > 0.20:
        return 40
    return 24


def auto_pick_mode(scores, times=None):
    """Winner mode ("algo:N") of a {algo: grain share} map.

    Filters slower than AUTO_TIME_CAP per probe sample are out;
    below AUTO_OFF_BELOW nothing is worth removing (None = off).
    Never raises.
    """
    try:
        items = list(scores.items())
    except (ValueError, TypeError, AttributeError):
        return None
    ranked = []
    for algo, g in items:
        try:
            gf = float(g)
        except (ValueError, TypeError):
            continue
        if not math.isfinite(gf):
            continue
        if times:
            try:
                if float(times.get(algo, 0.0)) > AUTO_TIME_CAP:
                    continue
            except (ValueError, TypeError, AttributeError):
                pass
        ranked.append((gf, algo))
    if not ranked:
        return None
    best_g, best_a = max(ranked)
    if best_g < AUTO_OFF_BELOW:
        return None
    return f"{best_a}:{grain_level_for(best_g)}"


def measure_grain(path, duration, progress=None):
    """Probe grain removal per candidate filter.

    Returns (scores, times): scores maps algo to the median
    1 - filtered/raw size share over short samples; times maps algo
    to filter-only seconds on the first sample. Encodes each sample
    once raw and once per candidate with a fast encoder. progress, if
    given, is started at once and told each sample (a _Spinner fits).
    Never raises, never writes near the film.
    """
    def _show(text=None, done=False, step=False):
        if progress is None:
            return
        try:
            if done:
                progress.finish()
            elif step:
                progress.tick()
            elif text is None:
                progress.start()
            else:
                progress.note(text)
        except Exception:
            pass
    empty = ({a: None for a in FILTER_CANDIDATES},
             {a: None for a in FILTER_CANDIDATES})
    if not path or not os.path.isfile(path):
        return empty
    if duration is None or duration <= 0:
        return empty
    _show()
    fracs = list(AUTO_PROBE_FRACS)
    per_algo = {a: [] for a in FILTER_CANDIDATES}
    times = {}
    tmp = None
    try:
        tmp = tempfile.mkdtemp(prefix="mkv_grain_")
        _register_dir(tmp)
        for fi, frac in enumerate(fracs):
            _show(f"probe {fi + 1}/{len(fracs)}")
            win = min(float(AUTO_PROBE_SECS), max(2.0, duration))
            last = max(0.0, duration - win)
            start = round(min(last, max(0.0, frac * duration - win / 2)), 1)
            base = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.1f}",
                    "-i", path, "-t", f"{win:.1f}", "-an",
                    "-c:v", "libsvtav1", "-preset", AUTO_PROBE_PRESET,
                    "-crf", AUTO_PROBE_CRF]
            if fi == 0:
                # Time every filter even when the raw probe fails, so
                # the time cap always applies.
                for algo, vf in FILTER_CANDIDATES.items():
                    _show(f"{algo}, time")
                    t0 = time.monotonic()
                    run_quiet(["ffmpeg", "-y", "-v", "error",
                               "-ss", f"{start:.1f}", "-i", path,
                               "-t", f"{win:.1f}", "-vf", vf,
                               "-an", "-f", "null", "-"])
                    times[algo] = round(time.monotonic() - t0, 1)
                    _show(step=True)
            raw = os.path.join(tmp, f"raw_{fi}.ivf")
            if run_quiet(base + [raw]).returncode != 0:
                _show(step=True)
                continue
            _show(step=True)
            try:
                rs = os.path.getsize(raw)
            except OSError:
                continue
            if rs <= 0:
                continue
            for algo, vf in FILTER_CANDIDATES.items():
                parts = vf.split("=", 1)
                disp = (f"{algo}, {parts[1]}"
                        if len(parts) > 1 else algo)
                _show(disp)
                flt = os.path.join(tmp, f"flt_{fi}_{algo}.ivf")
                if run_quiet(base + ["-vf", vf, flt]).returncode != 0:
                    _show(step=True)
                    continue
                _show(step=True)
                try:
                    fs = os.path.getsize(flt)
                except OSError:
                    continue
                per_algo[algo].append(1.0 - fs / rs)
    except Exception:
        return empty
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
            _unregister_dir(tmp)
        _show(done=True)
    out = {}
    for algo, vals in per_algo.items():
        if not vals:
            out[algo] = None
            continue
        vals.sort()
        out[algo] = vals[len(vals) // 2]
    return out, times


def format_grain_result(mode, share):
    """Final filter line, audio-result style: `- hqdn3d, 2:2:4:4 (12%)`.

    mode is a resolved denoise mode ("algo:N"); share is the winner's
    measured removable fraction. Off is reported bare.
    """
    if not mode:
        return "- off"
    try:
        vf = vfilter_for_denoise(mode)
    except (ValueError, TypeError):
        vf = None
    if not vf:
        return "- off"
    algo = denoise_algo(mode)
    name, _, params = vf.partition("=")
    if algo:
        disp = f"{algo}, {params}" if params else algo
    else:
        disp = vf
    if share is None:
        return f"- {disp}"
    try:
        pct = int(round(float(share) * 100))
    except (ValueError, TypeError):
        return f"- {disp}"
    return f"- {disp} ({pct}%)"


def interp_br(lo_br, lo_score, hi_br, hi_score, target_db):
    """Bitrate where a straight line through two measured points hits target.

    Returns ceil() of the crossing as an int, or None when the points
    do not bracket the target (caller falls back to hi_br). Opus takes
    any integer kbit/s, so the pick is exact, not ladder-quantized.
    Never raises.
    """
    try:
        lo_br, hi_br = int(lo_br), int(hi_br)
        lo_s, hi_s = float(lo_score), float(hi_score)
        tgt = float(target_db)
    except (ValueError, TypeError):
        return None
    if not (math.isfinite(lo_s) and math.isfinite(hi_s)
            and math.isfinite(tgt)):
        return None
    if hi_br <= lo_br or hi_s <= lo_s:
        return None
    if not (lo_s < tgt <= hi_s):
        return None
    cand = math.ceil(lo_br + (tgt - lo_s) * (hi_br - lo_br) / (hi_s - lo_s))
    if cand < lo_br + 1 or cand >= hi_br:
        return None
    return cand


def pick_audio_bitrate(path, track, target_db, duration, progress=None):
    """Lowest Opus bitrate for the target SI-SDR.

    Returns (bitrate_kbit, method, sisdr_db|None, size_pct|None) —
    the same order the audio plan is cached in. method is:
    search — an exact bitrate (any integer kbit/s, not a ladder rung)
    verified to hit the target on its hard place; copy — the track is
    kept as is (already opus, target out of reach, or measuring
    failed). size_pct is the chosen track size vs the original (100
    for copy, None when the source bitrate is unknown). A hard place
    that cannot hit the target even at the ladder top is left out of
    the maximum with a warning on stderr.
    """
    ch = track.get("channels") or 0
    codec = track.get("codec") or ""
    src_br = track.get("bit_rate")
    lo, hi = bitrate_bounds(track)
    cands = ladder_for(lo, hi) if ch > 0 else [64]
    steps_left = RANK_POSITIONS + REFINE_TOP * REFINE_PROBES
    ai = track.get("aindex")
    dbg(f"track a{ai}: {codec}, {ch} ch, source bitrate "
        + (f"~{src_br // 1000}k" if src_br else "unknown")
        + f"; search range {lo}..{hi}k")

    def show(text):
        if progress is None:
            return
        try:
            progress.stage(text)
        except Exception:
            pass

    show("start")

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

    # Without the SI-SDR meter there is no honest search: re-encoding
    # blind is a loss, keep the track as is.
    show("check")
    if run_quiet(["ffmpeg", "-hide_banner", "-h",
                  "filter=asisdr"]).returncode != 0:
        dbg(f"track a{ai}: no asisdr filter, quality cannot be measured "
            f"-- copying track as is")
        tick(consume=steps_left)
        return None, "copy", None, 100

    # Bitrate unknown (normal for audio in MKV): guess it with a fast
    # sample copy, so a lossy source is never "upscaled" and the chosen
    # size can be reported against the original track.
    # Lossless guesses are always above the search ceiling, so no cap there.
    est_br = None
    if src_br is None:
        show("estimate")
        est_br = estimate_src_bitrate(path, track["aindex"], start,
                                      sample_dur)
        if est_br and codec not in LOSSLESS_CODECS:
            dbg(f"track a{ai}: bitrate guessed from sample: "
                f"~{est_br // 1000}k -- search ceiling lowered")
            hi = min(hi, max(lo, math.ceil(est_br / 1000)))
            cands = ladder_for(lo, hi)
        elif est_br:
            dbg(f"track a{ai}: lossless source (~{est_br // 1000}k), "
                f"ladder kept whole")

    eff_br = src_br if src_br else est_br

    def pct_of(bitrate_k):
        """New track size vs the original, in percent.

        Same duration, so size ratio equals bitrate ratio:
        100 * new_size / old_size == 100 * new_bitrate / old_bitrate.
        """
        return size_percent(
            bitrate_k * 1000.0 if bitrate_k is not None else None, eff_br)

    # Opus that is already good enough (bitrate at or below need) — copy it.
    if codec == "opus" and eff_br:
        if math.ceil(eff_br / 1000) <= hi:
            dbg(f"track a{ai}: already opus (~{eff_br // 1000}k) -- "
                f"copying without re-encode")
            tick(bitrate_k=math.ceil(eff_br / 1000), consume=steps_left)
            return None, "copy", None, 100

    if ch <= 0:
        dbg(f"track a{ai}: channel count unknown -- copying as is")
        tick(consume=steps_left)
        return None, "copy", None, 100

    dbg(f"track a{ai}: ladder: " + ", ".join(map(str, cands)) + "k; "
        f"target {target_db:.1f} dB; rank {RANK_POSITIONS}x{RANK_SECS:g}s, "
        f"refine top {REFINE_TOP}x{REFINE_SECS:g}s")

    def rank_starts(total_dur, win):
        """Evenly spaced sample starts across the film. Never raises."""
        try:
            if total_dur is None or total_dur <= 0 or win <= 0:
                return [0.0]
            last = max(0.0, total_dur - win)
            if last <= 0:
                return [0.0]
            out, seen = [], set()
            for i in range(RANK_POSITIONS):
                frac = 0.02 + 0.96 * (i / max(1, RANK_POSITIONS - 1))
                s = round(min(last, max(0.0, frac * total_dur - win / 2)), 1)
                if s not in seen:
                    seen.add(s)
                    out.append(s)
            return sorted(out) or [0.0]
        except Exception:
            return [0.0]

    rank_win = min(float(RANK_SECS), max(1.0, duration))
    refine_win = min(float(REFINE_SECS), max(2.0, duration))
    probe_br = cands[len(cands) // 2]

    tmp = None
    try:
        tmp = tempfile.mkdtemp(prefix="mkv_audio_")
        _register_dir(tmp)

        def probe_at(ref_wav, active, bitrate_k, tag):
            """Encode + measure one step; None when it fails. Never raises."""
            try:
                enc = os.path.join(tmp, f"enc_{bitrate_k}_{tag}.opus")
                if not encode_opus(ref_wav, bitrate_k, enc):
                    return None
                score = measure_sisdr(ref_wav, enc, active)
                if score is None or not math.isfinite(score):
                    return None
                return score
            except Exception:
                return None

        # Stage 1: rank many short places by one probe bitrate.
        show("rank")
        ranked = []  # (hardness, center); lower score = harder
        for ri, rs in enumerate(rank_starts(duration, rank_win)):
            ref = os.path.join(tmp, f"rank_{ri}.wav")
            if not extract_sample(path, track["aindex"], rs, rank_win,
                                  ref, channels=track.get("channels") or 0):
                ranked.append((float("-inf"), rs + rank_win / 2))
                tick(bitrate_k=probe_br)
                continue
            try:
                active = active_channels(ref)
            except Exception:
                active = None
            score = probe_at(ref, active, probe_br, f"r{ri}")
            tick(bitrate_k=probe_br, score=score)
            ranked.append((score if score is not None else float("-inf"),
                           rs + rank_win / 2))
        ranked.sort(key=lambda t: t[0])
        hard = ranked[:max(1, REFINE_TOP)]
        dbg(f"track a{ai}: hardest centers: "
            + ", ".join(f"{c:.0f}s" for _s, c in hard))

        # Stage 2: bisect the ladder on the hardest places.
        show("refine")
        needs = []  # (need_br, need_score)
        for ji, (_hardness, center) in enumerate(hard):
            rs = round(min(max(0.0, duration - refine_win),
                           max(0.0, center - refine_win / 2)), 1)
            ref = os.path.join(tmp, f"refine_{ji}.wav")
            if not extract_sample(path, track["aindex"], rs, refine_win,
                                  ref, channels=track.get("channels") or 0):
                dbg(f"track a{ai}: refine sample {ji} failed, skipped")
                continue
            try:
                active = active_channels(ref)
            except Exception:
                active = None
            measured = {}

            def ask(idx):
                if idx in measured:
                    return measured[idx]
                br = cands[idx]
                s = probe_at(ref, active, br, f"f{ji}")
                tick(bitrate_k=br, score=s)
                measured[idx] = s
                return s

            lo, hi_idx = 0, len(cands) - 1
            ans, used = None, 0
            mark = steps_left
            while lo <= hi_idx and used < BISECT_PROBES:
                mid = (lo + hi_idx) // 2
                used += 1
                s = ask(mid)
                if s is not None and s >= target_db:
                    ans, hi_idx = mid, mid - 1
                else:
                    lo = mid + 1
            if ans is None:
                dbg(f"track a{ai}: place {ji}: target out of reach "
                    f"(top {cands[-1]}k) -- left out of the maximum")
                eprint(f"Note: track a{ai} place {ji + 1}: even "
                       f"{cands[-1]}k misses the target -- sizing for "
                       f"the reachable hard places.")
                # keep the bar honest: spend this place's steps
                tick(consume=REFINE_PROBES - (mark - steps_left))
                continue
            # Walk down to the true first hitting rung: bisection may
            # stop above it when the probe budget runs out.
            for _ in range(WALK_PROBES):
                if ans <= 0:
                    break
                s = ask(ans - 1)
                if s is not None and s >= target_db:
                    ans -= 1
                    continue
                break
            hi_br, hi_s = cands[ans], measured[ans]
            los = [i for i in measured
                   if i < ans and measured[i] is not None
                   and measured[i] < target_db]
            if los:
                # Secant steps on the measured bracket: hi stays a
                # verified hit, so the result never undershoots.
                lo_br = cands[max(los)]
                lo_s = measured[max(los)]
                for step in range(SECANT_PROBES):
                    cand = interp_br(lo_br, lo_s, hi_br, hi_s,
                                     target_db)
                    if cand is None or cand >= hi_br or cand <= lo_br:
                        break
                    s = probe_at(ref, active, cand, f"v{ji}_{step}")
                    tick(bitrate_k=cand, score=s)
                    if s is None:
                        break
                    if s >= target_db:
                        hi_br, hi_s = cand, s
                    else:
                        lo_br, lo_s = cand, s
            needs.append((hi_br, hi_s))
            dbg(f"track a{ai}: place {ji}: need {hi_br}k "
                f"(SI-SDR {hi_s:.1f} dB, exact)")
        if not needs:
            dbg(f"track a{ai}: target out of reach -- copying track")
            tick(bitrate_k=cands[-1], consume=steps_left)
            return None, "copy", None, 100
        # Maximum of the hard places: the whole track gets the
        # bitrate the hardest checked segment needs, so every
        # checked segment meets the target.
        ordered = sorted(b for b, _s in needs)
        pick_br = ordered[-1]
        pick_score = next(s for b, s in needs if b == pick_br)
        if eff_br and eff_br <= pick_br * 1000:
            dbg(f"track a{ai}: pick {pick_br}k at/above source -- "
                f"copying without re-encode")
            tick(bitrate_k=pick_br, score=pick_score, consume=steps_left)
            return None, "copy", None, 100
        if eff_br and pick_br * 1000 > eff_br * SAT_MIN_SAVING:
            dbg(f"track a{ai}: pick {pick_br}k would save less than "
                f"a quarter -- copying track as is")
            tick(bitrate_k=pick_br, score=pick_score, consume=steps_left)
            return None, "copy", None, 100
        dbg(f"track a{ai}: picked {pick_br}k -- max of {len(hard)} "
            f"hard places ({pct_of(pick_br)}% of source)")
        tick(bitrate_k=pick_br, score=pick_score, consume=steps_left)
        return pick_br, "search", _json_safe_score(pick_score), pct_of(pick_br)
    except Exception as e:
        # Sampling or measuring failed: keep the track as is.
        dbg(f"track a{ai}: search failed ({e}) -- copying track")
        tick(consume=steps_left)
        return None, "copy", None, 100
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
            _unregister_dir(tmp)


def format_audio_result(br, method, score, pct, aindex=None):
    """Final per-track line: `- sound 1: vbr 48 SI-SDR 32 (20%)`.

    Takes one audio-plan entry: br -- chosen Opus bitrate (kbit/s);
    score -- measured SI-SDR (dB), shown on the subjective --sdr scale
    (0..100); pct -- new audio size vs the original track, that is
    100 * new size / old size like ab-av1 (100 for copy); aindex --
    zero-based track index, shown one-based, so every line says which
    stream it belongs to; None for a lone track ("- sound: ...").
    Missing parts are skipped.
    """
    try:
        tag = (f"- sound {int(aindex) + 1}: " if aindex is not None
               else "- sound: ")
    except (ValueError, TypeError):
        tag = "- "
    if method == "copy":
        return f"{tag}copy (100%)"
    if br is None:
        return tag.rstrip()
    parts = [f"{tag}vbr", str(int(br))]
    if score is not None and math.isfinite(score):
        parts += ["SI-SDR", str(int(round(sisdr_to_sdr(score))))]
    if pct is not None:
        parts.append(f"({int(round(pct))}%)")
    return " ".join(parts)


def _track_steps():
    """Progress-bar step count: rank probes + refine probes."""
    return RANK_POSITIONS + REFINE_TOP * REFINE_PROBES


def abav1_hint_text(path, crf, svt_args=None, vfilter=None):
    """Rebuild ab-av1's trailing `Encode with:` line for its length.

    Only the visible length matters (to clear wrapped rows too);
    the exact quoting is best-effort. Never raises.
    """
    try:
        parts = ["Encode with: ab-av1 encode -i", str(path),
                 "--crf", str(crf), "--preset", str(PRESET)]
        if isinstance(vfilter, str) and vfilter.strip():
            parts += ["--vfilter", f'"{vfilter.strip()}"']
        for _a in (svt_args or []):
            if isinstance(_a, str) and _a.strip():
                parts += ["--svt", _a.strip()]
        return " ".join(parts)
    except Exception:
        return ""


def clear_abav1_hint(path, crf, svt_args=None, vfilter=None):
    """Erase ab-av1's trailing `Encode with:` line from a real tty.

    ab-av1 always prints it after a successful search, between our
    bars. Only runs on a color tty (never pipes, files, dumb
    terminals); a wrong row guess at worst leaves today's one stale
    line. Never raises.
    """
    try:
        if not _use_color():
            return
        width = os.get_terminal_size(sys.stderr.fileno()).columns
        if not width or width < 40:
            width = 80
    except Exception:
        return
    try:
        text = abav1_hint_text(path, crf, svt_args, vfilter)
        if not text:
            return
        rows = max(1, (len(text) + width - 1) // width)
        sys.stderr.write(f"\x1b[{rows}A\x1b[J")
        sys.stderr.flush()
    except Exception:
        pass


class _HintFilter:
    """Drop ab-av1's `Encode with:` stderr line, pass all else.

    ab-av1 draws its bar with carriage returns (no newlines) and
    prints the hint as one newline-terminated line, so per-line
    filtering is exact: no length guessing, no residue. Undecided
    bytes are held briefly; failures forward everything (fail-open),
    so a parser bug can only leak the hint, never eat the bar.
    Never raises.
    """

    PREFIX = b"Encode with:"

    def __init__(self):
        self._line_start = True
        self._suppress = False
        self._hold = b""

    def feed(self, data):
        """Forwardable bytes from one stderr chunk."""
        try:
            if not data:
                return b""
            buf = self._hold + bytes(data)
            self._hold = b""
        except Exception:
            return b""
        try:
            out = bytearray()
            i, n = 0, len(buf)
            while i < n:
                if self._suppress:
                    j = buf.find(b"\n", i)
                    if j < 0:
                        i = n
                    else:
                        i = j + 1
                        self._suppress = False
                        self._line_start = True
                    continue
                if self._line_start:
                    need = len(self.PREFIX)
                    if n - i < need:
                        if self.PREFIX.startswith(buf[i:]):
                            self._hold = buf[i:]
                            break
                        self._line_start = False
                    if buf.startswith(self.PREFIX, i):
                        self._suppress = True
                        continue
                    self._line_start = False
                j = n
                for sep in (b"\r", b"\n"):
                    k = buf.find(sep, i)
                    if k >= 0:
                        j = min(j, k)
                out += buf[i:j]
                if j < n:
                    out += buf[j:j + 1]
                    i = j + 1
                    self._line_start = True
                else:
                    i = n
            return bytes(out)
        except Exception:
            self._suppress = False
            self._line_start = True
            try:
                return bytes(data)
            except Exception:
                return b""

    def flush(self):
        """Leftover held bytes (stream end); forwarded as is."""
        try:
            held, self._hold = self._hold, b""
            return bytes(held)
        except Exception:
            return b""


def _pump_pty(master):
    """Relay a pty master to real stderr minus the ab-av1 hint line.

    Ends at EOF (the child closed its slave end). Never raises.
    """
    filt = _HintFilter()
    try:
        err_fd = sys.stderr.fileno()
    except Exception:
        err_fd = 2
    try:
        while True:
            try:
                data = os.read(master, 65536)
            except Exception:
                break
            if not data:
                break
            try:
                out = filt.feed(data)
                if out:
                    os.write(err_fd, out)
            except (OSError, ValueError):
                break
    finally:
        try:
            tail = filt.flush()
            if tail:
                os.write(err_fd, tail)
        except (OSError, ValueError):
            pass
        try:
            os.close(master)
        except OSError:
            pass


def _spawn_pty(cmd, **kwargs):
    """Popen with stderr on a sized pty; returns (proc, master_fd).

    The slave gets the real stderr window size, so ab-av1 draws its
    full-width bar. Raises on any setup failure (caller falls back
    to plain inherit). Never half-opens: fds are closed on failure.
    """
    import pty as _pty
    import fcntl as _fcntl
    import termios as _termios
    import struct as _struct
    master, slave = _pty.openpty()
    try:
        try:
            size = os.get_terminal_size(sys.stderr.fileno())
            rows, cols = max(1, size.lines), max(40, size.columns)
        except (OSError, ValueError, AttributeError):
            rows, cols = 24, 80
        _fcntl.ioctl(slave, _termios.TIOCSWINSZ,
                     _struct.pack("HHHH", rows, cols, 0, 0))
        proc = _spawn(cmd, stderr=slave, **kwargs)
    except BaseException:
        try:
            os.close(slave)
        except OSError:
            pass
        try:
            os.close(master)
        except OSError:
            pass
        raise
    try:
        os.close(slave)  # parent end: EOF arrives when the child dies
    except OSError:
        pass
    return proc, master


def run_crf_search(path, min_vmaf, svt_args=None, vfilter=None,
                   ref_vfilter=None):
    """ab-av1 crf-search; returns (crf, samples, no_good_crf).

    svt_args is a list of SVT key=value pairs (see
    svt_args_for_denoise()); vfilter is one ffmpeg filter string (see
    vfilter_for_denoise()); ref_vfilter overrides the VMAF reference
    filter (--reference-vfilter), so denoise loss is scored instead
    of hidden. The same denoise must score the samples and
    the final encode, so the found CRF fits what is encoded.

    crf is None on failure. samples collects the (vmaf, percent) pair of
    every `sample-encode-done` event — percent is the predicted encode
    size vs the source. no_good_crf is True when ab-av1 reported
    "Failed to find a suitable crf" (the quality target needs more than
    --max-encoded-percent, 80% of the source) rather than crashing.

    On a real screen ab-av1 draws on a sized pty whose output is
    relayed to stderr verbatim, except its trailing `Encode with:`
    hint line, which is filtered out exactly (per-line match, no
    length guessing). No tty (or relay setup failure): stderr is
    inherited and the hint is erased afterwards as a fallback;
    in --debug stderr is piped and drained. stdout is captured as
    NDJSON (--stdout-format json) to read the final CRF without
    stealing the terminal from the progress UI.

    Samples go to a private --temp-dir we own and delete on abort / exit,
    so a Ctrl+C never leaves .ab-av1-* junk next to the movie.
    """
    if not path or min_vmaf is None:
        return None, [], False
    temp_dir = ensure_abav1_temp()
    cmd = ["ab-av1", "crf-search", "-i", path, "--preset", str(PRESET),
           "--min-vmaf", str(min_vmaf), "--stdout-format", "json",
           "--temp-dir", temp_dir]
    eff_svt = list(svt_args) if svt_args else []
    for _a in eff_svt:
        if not isinstance(_a, str) or "=" not in _a:
            continue
        cmd += ["--svt", _a]
    if isinstance(vfilter, str) and vfilter.strip():
        cmd += ["--vfilter", vfilter.strip()]
    if isinstance(ref_vfilter, str) and ref_vfilter.strip():
        cmd += ["--reference-vfilter", ref_vfilter.strip()]
    pump = None
    try:
        if DEBUG:
            # debug log: no bars at all; ab-av1's own stderr is drained
            # in the background (INFO lines are noise for the log)
            p = _spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, bufsize=1)
        else:
            try:
                # Real screen: ab-av1 draws its bar on a sized pty we
                # relay to stderr minus its trailing hint line.
                # (Piping stderr directly makes ab-av1 fall back to
                # INFO log lines instead of the bar.)
                if not _use_color():
                    raise OSError("no tty for the relay")
                p, _master = _spawn_pty(cmd, stdout=subprocess.PIPE,
                                        text=True, bufsize=1)
                pump = threading.Thread(target=_pump_pty, args=(_master,),
                                        daemon=True)
                pump.start()
            except Exception:
                # plain inherit + erase the hint afterwards
                p = _spawn(cmd, stdout=subprocess.PIPE, stderr=None,
                           text=True, bufsize=1)
    except FileNotFoundError:
        return None, [], False
    _CHILD_PROCS.append(p)
    if DEBUG and p.stderr:
        def _drain_err(pipe):
            try:
                for _line in pipe:
                    pass  # INFO / progress lines: not part of the log
            except Exception:
                pass
        threading.Thread(target=_drain_err, args=(p.stderr,),
                         daemon=True).start()
    found = None
    samples = []
    no_good = False
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
            kind = msg.get("type")
            if kind == "sample-encode-done" or (
                    kind is None and "predicted_encode_percent" in msg):
                # One event per crf attempt; pre-0.11.5 events lack `type`.
                try:
                    vmaf = float(msg["vmaf"])
                    pct = float(msg.get("predicted_encode_percent"))
                    if math.isfinite(vmaf) and math.isfinite(pct):
                        samples.append((vmaf, pct))
                        dbg(f"  try: crf {msg.get('crf')}: VMAF "
                            f"{vmaf:.2f}, size {pct:.0f}% of source")
                except (KeyError, TypeError, ValueError):
                    pass
            elif kind == "crf-search-done":
                try:
                    found = float(msg["crf"])
                except (KeyError, TypeError, ValueError):
                    pass
                try:
                    dbg(f"  done: crf {found}, predicted size "
                        f"{float(msg.get('predicted_encode_percent')):.0f}% "
                        f"of source")
                except (TypeError, ValueError):
                    pass
            elif kind == "crf-search-error":
                no_good = True
                dbg("  search gave up: target needs over 80% "
                    "of source size")
        p.wait()
    except BaseException:
        # Never leave an orphan encoder burning CPU on abort.
        try:
            if p.pid:
                os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
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
        if pump is not None:
            pump.join(timeout=10)
        if p in _CHILD_PROCS:
            _CHILD_PROCS.remove(p)
    if p.returncode != 0:
        return None, samples, no_good
    if found is not None and not DEBUG and pump is None:
        clear_abav1_hint(path, found, eff_svt,
                         vfilter if isinstance(vfilter, str) else None)
    return found, samples, no_good


def load_cache(cache_path):
    """Read (crf, audio_plan, video) from a cache file.

    Returns (None, None, None) if bad. crf may be null when only the
    audio plan was saved so far; video is "copy" when the video stream
    was decided to be kept as is. Audio entries are (br, method, score,
    pct); caches from older versions store (br, method) with the two
    extra fields unknown, and the old ceiling/fallback methods are
    normalized to the new policy: the track is copied, not re-encoded.
    /tmp is world-writable: every entry is validated so a broken or
    planted cache file cannot crash us.
    """
    try:
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        raw_crf = cached.get("crf")
        if raw_crf is not None and isinstance(raw_crf, bool):
            raise ValueError("bad crf")
        crf = float(raw_crf) if raw_crf is not None else None
        if crf is not None and not math.isfinite(crf):
            raise ValueError("bad crf")
        raw_plan = cached.get("audio", [])
        if not isinstance(raw_plan, list):
            raise ValueError("bad audio plan")
        plan = []
        for a in raw_plan:
            if not isinstance(a, (list, tuple)) or len(a) not in (2, 4):
                raise ValueError("bad cache entry")
            br, method = a[0], a[1]
            score = a[2] if len(a) == 4 else None
            pct = a[3] if len(a) == 4 else None
            if br is not None and (isinstance(br, bool)
                                   or not isinstance(br, int)):
                raise ValueError("bad cache entry")
            if method not in ("copy", "search", "max", "fallback"):
                raise ValueError("bad cache entry")
            for val in (score, pct):
                if val is None:
                    continue
                if (isinstance(val, bool)
                        or not isinstance(val, (int, float))
                        or not math.isfinite(val)):
                    raise ValueError("bad cache entry")
            if method in ("max", "fallback"):
                # old policy kept the ceiling / guessed bitrate;
                # the new one copies the track instead
                br, method = None, "copy"
            plan.append((br, method,
                         None if score is None else round(float(score), 1),
                         None if pct is None else int(round(pct))))
        video = cached.get("video")
        if video is not None and video != "copy":
            raise ValueError("bad video entry")
        return crf, plan, video
    except (OSError, ValueError, TypeError, KeyError,
            IndexError, AttributeError):
        return None, None, None


def noise_cache_path(filename, filesize):
    """Verdict-cache file for the --noise race; "" when unusable."""
    try:
        if not filename or filesize is None:
            return ""
        return os.path.join(CACHE_DIR,
                            f"{filename}.{int(filesize)}b.noise.json")
    except (ValueError, TypeError):
        return ""


def load_noise_verdict(cache_path):
    """Read {"mode", "share"} of a --noise race; None when bad.

    mode is a resolved denoise mode ("algo:N"); None mode means the
    race decided the grain is not worth removing. /tmp is
    world-writable: the file is validated so a broken or planted one
    cannot crash us or smuggle in a bad filter.
    """
    try:
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        if not isinstance(cached, dict):
            raise ValueError("bad verdict file")
        if cached.get("v") != NOISE_VERDICT_VERSION:
            raise ValueError("stale verdict version")
        mode = cached.get("mode")
        if mode is not None:
            if not isinstance(mode, str) or not mode.strip():
                raise ValueError("bad verdict mode")
            if denoise_algo(mode) not in FILTER_CANDIDATES:
                raise ValueError("bad verdict mode")
        share = cached.get("share")
        if share is not None and (isinstance(share, bool)
                                  or not isinstance(share, (int, float))
                                  or not math.isfinite(share)):
            raise ValueError("bad verdict share")
        return {"mode": mode, "share": share}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def save_noise_verdict(cache_path, mode, share):
    """Remember a --noise race verdict; silently skip on failure."""
    if not cache_path:
        return
    try:
        payload = {"v": NOISE_VERDICT_VERSION,
                   "mode": mode,
                   "share": (None if share is None
                             else round(float(share), 3))}
    except (ValueError, TypeError):
        return
    try:
        with open(cache_path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(cache_path + ".tmp", cache_path)
    except (OSError, ValueError, TypeError):
        pass


def crop_cache_path(filename, filesize):
    """Verdict-cache file for the --crop probe; "" when unusable."""
    try:
        if not filename or filesize is None:
            return ""
        return os.path.join(CACHE_DIR,
                            f"{filename}.{int(filesize)}b.crop.json")
    except (ValueError, TypeError):
        return ""


def load_crop_verdict(cache_path):
    """Read {"area"} of a --crop probe; None when bad.

    area is a "w:h:x:y" bar area; None area means the probe found no
    bars worth cutting. Validated like every /tmp file. Never raises.
    """
    try:
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        if not isinstance(cached, dict):
            raise ValueError("bad verdict file")
        area = cached.get("area")
        if area is not None:
            if not isinstance(area, str):
                raise ValueError("bad verdict area")
            parts = area.strip().split(":")
            if len(parts) != 4:
                raise ValueError("bad verdict area")
            w, h, x, y = (int(v) for v in parts)
            if w <= 0 or h <= 0 or x < 0 or y < 0:
                raise ValueError("bad verdict area")
            area = f"{w}:{h}:{x}:{y}"
        return {"area": area}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def save_crop_verdict(cache_path, area):
    """Remember a --crop probe verdict; silently skip on failure."""
    if not cache_path:
        return
    try:
        payload = {"area": (None if area is None else str(area))}
    except (ValueError, TypeError):
        return
    try:
        with open(cache_path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(cache_path + ".tmp", cache_path)
    except (OSError, ValueError, TypeError):
        pass


def make_cache_id(filename, filesize, min_vmaf, sdr, noise=False,
                  denoise=None, crop=None):
    """Cache key: file identity + quality targets (+ denoise mode).

    Denoise and crop runs get their own key space: the same CRF is
    not reused across filters or picture sizes, a filtered encode
    needs its own search. Keys without them stay exactly as before,
    so old caches load. denoise is a parsed mode ("algo", "algo:N"
    or None); crop is a "w:h:x:y" area. Suffix forms:
    ".noise{algo}{level}h", ".crop{W}x{H}+{x}+{y}".
    """
    if not filename or filesize is None:
        return ""
    cid = f"{filename}.{filesize}b.vmaf{min_vmaf}.sdr{sdr}"
    mode = denoise
    if mode is None and noise:
        mode = "hqdn3d"
    algo = denoise_algo(mode)
    if algo is not None:
        try:
            level = denoise_grain_level(mode)
        except Exception:
            level = None
        if level is not None:
            # "h" = honest VMAF reference (unfiltered source); older
            # masked caches never match.
            cid += f".noise{algo}{level}h"
    if isinstance(crop, str) and crop.strip():
        try:
            w, h, x, y = (int(v) for v in crop.strip().split(":")[:4])
            cid += f".crop{w}x{h}+{x}+{y}"
        except (ValueError, TypeError, IndexError):
            pass
    return cid


def free_memory_mb():
    """Free RAM and swap in MB from /proc/meminfo; (None, None) if unknown."""
    ram_mb, swap_mb = None, None
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            info = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(":"):
                    try:
                        info[parts[0][:-1]] = int(parts[1])
                    except (ValueError, TypeError):
                        pass
        if "MemAvailable" in info:
            ram_mb = info["MemAvailable"] // 1024
        if "SwapFree" in info:
            swap_mb = info["SwapFree"] // 1024
    except (OSError, ValueError):
        pass
    return ram_mb, swap_mb


def nothing_to_compress(video_copy, audio_plan):
    """True when every stream would be kept as is (nothing to compress)."""
    if not video_copy:
        return False
    return all(method == "copy"
               for _br, method, _score, _pct in audio_plan or [])


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
    ap.add_argument("--debug", action="store_true",
                    help="plain-language step log: request, checks, "
                         "choices, results; no fancy progress bars")
    ap.add_argument("--noise", "--denoise", action="store_true",
                    help="grain removal: probe the source, race the "
                         "filters, use the winner; no flag for no "
                         "denoise. Same filter goes to crf-search and "
                         "encode")
    ap.add_argument("--crop", action="store_true",
                    help="cut black bars: detect the picture area on "
                         "samples and crop it in crf-search and encode")
    args = ap.parse_args()

    global DEBUG
    DEBUG = args.debug

    try:
        denoise_mode = parse_denoise(args.noise)
    except ValueError:
        eprint("--noise takes no value")
        return 1
    if denoise_mode == "auto":
        # Resolved later after probing the source; never cached as is.
        denoise_svt, denoise_vf = [], None
    else:
        try:
            denoise_svt = svt_args_for_denoise(denoise_mode)
            denoise_vf = vfilter_for_denoise(denoise_mode)
        except ValueError:
            eprint("--noise takes no value")
            return 1

    if not args.input:
        eprint(f"Usage: {os.path.basename(sys.argv[0])} "
               f"[--vmaf N] [--sdr N] [--noise] [--crop] "
               f"[--debug] input.mkv")
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

    duration, tracks = None, None

    abs_input = os.path.realpath(args.input)
    filename = os.path.basename(abs_input)
    try:
        filesize = os.path.getsize(abs_input)
    except OSError as e:
        eprint(f"Cannot read file: {e}")
        return 1

    if not cleanup_stale_temps(abs_input):
        return 1

    # Overwrite checkpoint before any slow probe: answering "n" must
    # not cost minutes of grain/crop/audio search first.
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

    if denoise_mode == "auto":
        # Bare --noise: probe the source first, then pick the filter.
        # The resolved mode (never "auto") keys the cache below; the
        # verdict itself is remembered by file identity, so a repeat
        # run skips the slow race.
        duration, tracks = probe(abs_input)
        verdict_path = noise_cache_path(filename, filesize)
        verdict = (load_noise_verdict(verdict_path) if verdict_path
                   else None)
        if verdict is None:
            spin = None
            if not DEBUG:
                total = (len(AUTO_PROBE_FRACS)
                         * (1 + len(FILTER_CANDIDATES))
                         + len(FILTER_CANDIDATES))
                spin = _Spinner("probe", total=total)
            scores, ftimes = measure_grain(abs_input, duration,
                                           progress=spin)
            denoise_mode = auto_pick_mode(scores, ftimes)
            algo = denoise_algo(denoise_mode)
            share = scores.get(algo) if algo else None
            save_noise_verdict(verdict_path, denoise_mode, share)
        else:
            denoise_mode, share = verdict["mode"], verdict.get("share")
            dbg(f"noise verdict cached: {denoise_mode} ({share})")
        try:
            denoise_svt = svt_args_for_denoise(denoise_mode)
            denoise_vf = vfilter_for_denoise(denoise_mode)
        except ValueError:
            denoise_mode, denoise_svt, denoise_vf = None, [], None
        algo = denoise_algo(denoise_mode)
        eprint(format_grain_result(denoise_mode, share))

    if ((denoise_mode == "auto" or args.crop)
            and (duration is None or tracks is None)):
        duration, tracks = probe(abs_input)
    crop_area = None
    if args.crop:
        verdict_path = crop_cache_path(filename, filesize)
        verdict = (load_crop_verdict(verdict_path) if verdict_path
                   else None)
        if verdict is None:
            cspin = None
            if not DEBUG:
                cspin = _Spinner("crop", total=len(CROP_FRACS))
            crop_area = detect_crop(abs_input, duration, progress=cspin)
            save_crop_verdict(verdict_path, crop_area)
        else:
            crop_area = verdict.get("area")
            dbg(f"crop verdict cached: {crop_area}")
        eprint(f"- crop {crop_area}" if crop_area else "- crop off")
    crop_vf = f"crop={crop_area}" if crop_area else None
    video_vf = ",".join(v for v in (crop_vf, denoise_vf) if v) or None
    # Honest VMAF reference: ab-av1 scores the encode against the
    # filtered reference by default, which hides the quality the
    # denoise filter itself removes (measured: ~6 points on real
    # content). The reference keeps the crop (sizes must match) but
    # never the denoise; "null" is a pass-through. Crop-only runs
    # need no override: both sides are already identical.
    if denoise_vf and crop_vf:
        ref_vf = crop_vf
    elif denoise_vf:
        ref_vf = "null"
    else:
        ref_vf = None

    dbg(f"request: {abs_input} ({filesize} bytes)")
    if denoise_mode is not None:
        _lvl = denoise_grain_level(denoise_mode)
        _svt_s = ", ".join(denoise_svt)
        _vf_s = f", filter {denoise_vf}" if denoise_vf else ""
        dbg(f"targets: VMAF >= {min_vmaf:g}; SI-SDR >= {min_sisdr:g} dB "
            f"(--sdr {args.sdr:g}); grain: {denoise_algo(denoise_mode)} "
            f"film-grain={_lvl} ({_svt_s}{_vf_s})")
    else:
        dbg(f"targets: VMAF >= {min_vmaf:g}; SI-SDR >= {min_sisdr:g} dB "
            f"(--sdr {args.sdr:g}); no --denoise")

    cache_id = make_cache_id(filename, filesize, min_vmaf, args.sdr,
                             denoise=denoise_mode, crop=crop_area)
    cache_path = os.path.join(CACHE_DIR, cache_id + ".json")
    # Legacy caches from older flag names / bash wrapper.
    legacy_cache = os.path.join(
        CACHE_DIR,
        f"{filename}.{filesize}b.vmaf{min_vmaf}.sisdr{min_sisdr}.json")
    legacy_crf = os.path.join(CACHE_DIR,
                              f"{filename}.{filesize}b.vmaf{min_vmaf}.crf")

    base, ext = os.path.splitext(abs_input)
    tmp_output = base + "_tmp_encode" + ext

    # --- cache ---
    crf, audio_plan, cached_video = None, None, None
    if os.path.isfile(cache_path):
        dbg(f"cache: reading {cache_path}")
        crf, audio_plan, cached_video = load_cache(cache_path)
    elif os.path.isfile(legacy_cache):
        dbg(f"cache: reading legacy {legacy_cache}")
        crf, audio_plan, cached_video = load_cache(legacy_cache)
    else:
        dbg("no cache -- full search needed")
    if audio_plan is not None:
        dbg("audio plan already cached, no search needed")
    # "copy" cached from a previous run: the video stream stays as is.
    video_copy = cached_video == "copy"

    if tracks is None:
        duration, tracks = probe(abs_input)

    # Cache from other tracks (same name/size, other streams) —
    # the audio plan is useless, search again.
    if audio_plan is not None and len(audio_plan) != len(tracks):
        audio_plan = None

    # --- 1. audio: bitrates (first — so the user sees this before ab-av1) ---
    if audio_plan is None:
        audio_plan = []
        for t in tracks:
            progress = (None if DEBUG
                          else _AudioProgress(_track_steps(),
                                             aindex=t.get("aindex")))
            if progress is not None:
                progress.start()
            try:
                br, method, score, pct = pick_audio_bitrate(
                    abs_input, t, min_sisdr, duration, progress=progress)
                # the updating search line becomes the result line
                tag_idx = t.get("aindex") if len(tracks) > 1 else None
                if progress:
                    progress.finish(
                        format_audio_result(br, method, score, pct,
                                            tag_idx))
                else:
                    eprint(format_audio_result(br, method, score, pct,
                                               tag_idx))
            except BaseException:
                if progress:
                    progress.finish()
                raise
            audio_plan.append((br, method, score, pct))
        # Persist audio plan early (crf may still be unknown).
        try:
            with open(cache_path + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"crf": crf, "audio": audio_plan}, f)
            os.replace(cache_path + ".tmp", cache_path)
        except OSError:
            pass
    elif tracks:
        # cached plan: show the same result lines without re-searching
        for t, entry in zip(tracks, audio_plan):
            tag_idx = t.get("aindex") if len(tracks) > 1 else None
            eprint(format_audio_result(*entry, tag_idx))

    # --- 2. video: CRF (or keep the video stream as is) ---
    if not video_copy:
        # a CRF found without denoise does not fit a grain-synthesis
        # encode, and the other way round (level matters too); a CRF
        # found on other pixels (crop) does not fit either
        if crf is None and denoise_mode is None and crop_area is None:
            if os.path.isfile(legacy_crf):
                try:
                    with open(legacy_crf, encoding="utf-8") as f:
                        crf = float(f.read().strip())
                    if not math.isfinite(crf):
                        raise ValueError("bad legacy crf")
                except (OSError, ValueError):
                    crf = None
        persist_copy = video_copy  # decided by an earlier run
        if crf is None:
            dbg(f"video: CRF search (preset {PRESET}"
                + (", " + video_vf if video_vf else "") + ")")
            crf, samples, no_good = run_crf_search(
                abs_input, min_vmaf, svt_args=denoise_svt,
                vfilter=video_vf, ref_vfilter=ref_vf)
            if crf is None:
                # Keep the video stream as is for this run either way.
                # A genuine miss is remembered; a crash is not, so the
                # next run searches again instead of reusing the miss.
                video_copy = True
                persist_copy = bool(no_good)
                if no_good:
                    if samples:
                        best_vmaf = max(v for v, _p in samples)
                        best_pct = next(
                            (pc for v, pc in samples if v == best_vmaf),
                            None)
                        detail = f"best sample VMAF {best_vmaf:.2f}"
                        if best_pct is not None:
                            detail += f", {best_pct:.0f}% of source"
                    else:
                        detail = "no usable sample"
                    eprint(f"Video: VMAF {min_vmaf:g} needs more than "
                           f"80% of the source size ({detail}). "
                           f"Keeping video as is; try lower --vmaf.")
                else:
                    if not os.path.isfile(abs_input):
                        eprint("Video: CRF search failed and the input "
                               "file is gone mid-run (drive asleep or "
                               "unplugged?). Keeping video as is.")
                    else:
                        try:
                            tmp_free = shutil.disk_usage(
                                tempfile.gettempdir()).free // (1024 * 1024)
                        except OSError:
                            tmp_free = None
                        hint = (f" (/tmp free: {tmp_free} MB)"
                                if tmp_free is not None else "")
                        eprint("Video: CRF search failed, "
                               "keeping video as is "
                               "(next run will search again)."
                               f"{hint}")
            else:
                persist_copy = False
        cached_out = ({"crf": None, "audio": audio_plan, "video": "copy"}
                      if (video_copy and persist_copy)
                      else {"crf": crf, "audio": audio_plan})
        try:
            with open(cache_path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(cached_out, f)
            os.replace(cache_path + ".tmp", cache_path)
        except OSError:
            pass
        if crf is not None and denoise_mode is None and crop_area is None:
            try:
                with open(legacy_crf, "w", encoding="utf-8") as f:
                    f.write(str(crf))
            except OSError:
                pass

    # --- nothing left to compress? ---
    if nothing_to_compress(video_copy, audio_plan):
        eprint("Nothing to compress: the video and every audio track "
               "already fit the targets, the file is left unchanged.")
        return 1
    if video_copy and denoise_mode is not None:
        eprint("Note: the video stream is kept as is, --noise is not "
               "applied to a copied stream.")

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
    if video_copy:
        # Keep the video stream byte-exact; audio still follows the
        # plan (same per-stream options ab-av1 would use).
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", abs_input,
               "-map", "0", "-dn", "-c:v", "copy", "-c:s", "copy",
               "-c:t", "copy"]
        if video_copy and not DEBUG:
            cmd += ["-progress", "pipe:1", "-nostats"]
        for t, (br, method, _score, _pct) in zip(tracks, audio_plan):
            idx = t["aindex"]
            if method == "copy":
                cmd += [f"-c:a:{idx}", "copy"]
                continue
            if (t["channels"] or 0) == 6:
                # True 6-channel tracks: 5.1(side) -> 5.1,
                # or the Opus mapping breaks on the side layout.
                cmd += [f"-mapping_family:a:{idx}", "1"]
                cmd += [f"-filter:a:{idx}",
                        "channelmap=channel_layout=5.1"]
            cmd += [f"-c:a:{idx}", "libopus", f"-b:a:{idx}", f"{br}k"]
        cmd += [tmp_output]
    else:
        cmd = ["ab-av1", "encode", "-i", abs_input, "-o", tmp_output,
               "--crf", str(crf), "--preset", str(PRESET)]
        for _a in denoise_svt:
            cmd += ["--svt", _a]
        if video_vf:
            cmd += ["--vfilter", video_vf]
        if tracks:
            cmd += ["--acodec", "libopus"]
            for t, (br, method, _score, _pct) in zip(tracks, audio_plan):
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

    if DEBUG:
        dbg("encode: " + " ".join(cmd))
    try:
        if DEBUG:
            # keep the log readable: no bar spam, status lines forwarded
            p = _spawn(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True, bufsize=1)
        elif video_copy:
            # silent ffmpeg: progress goes to the pipe, errors to screen
            p = _spawn(cmd, stdout=subprocess.PIPE, stderr=None,
                       text=True, bufsize=1)
        else:
            p = _spawn(cmd)
    except FileNotFoundError:
        eprint("ab-av1/ffmpeg not found. Install ab-av1 and ffmpeg.")
        cleanup_generated()
        return 1
    _CHILD_PROCS.append(p)
    if DEBUG and p.stderr:
        def _encode_status(pipe):
            try:
                for line in pipe:
                    line = line.rstrip()
                    if not line:
                        continue
                    if "fps" in line or "Encoded" in line:
                        eprint("  [encode] " + line[-90:])
            except Exception:
                pass
        threading.Thread(target=_encode_status, args=(p.stderr,),
                         daemon=True).start()
    spin = None
    if video_copy and not DEBUG:
        n_tracks = len(tracks) if tracks else 0
        total = duration if duration and duration > 0 else None
        spin = _Spinner("copying audio"
                        + (f" {n_tracks} tracks" if n_tracks else ""),
                        total=total)
        spin.start()
    if spin is not None and getattr(p, "stdout", None) is not None:
        def _copy_reader(pipe, spin, total):
            try:
                for line in pipe:
                    sec = _parse_progress_seconds(line)
                    if sec is not None:
                        if total:
                            sec = min(sec, total)
                        spin.tick(done=sec)
            except Exception:
                pass
            finally:
                try:
                    pipe.close()
                except Exception:
                    pass
        threading.Thread(target=_copy_reader,
                         args=(p.stdout, spin, spin.total),
                         daemon=True).start()
    try:
        if spin is None:
            rc = p.wait()
        else:
            while True:
                try:
                    rc = p.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    spin.pulse()
    except BaseException:
        try:
            if p.pid:
                os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                p.kill()
            except Exception:
                pass
        cleanup_generated()
        raise
    finally:
        if p in _CHILD_PROCS:
            _CHILD_PROCS.remove(p)
        if spin is not None:
            spin.finish()

    if rc != 0:
        cleanup_generated()
        eprint("Encode failed! Input file untouched.")
        ram_mb, swap_mb = free_memory_mb()
        if ram_mb is not None or swap_mb is not None:
            eprint(f"Free memory now: "
                   f"{ram_mb if ram_mb is not None else '?'} MB RAM, "
                   f"{swap_mb if swap_mb is not None else '?'} MB swap. "
                   f"A preset-3 1080p encode needs gigabytes; stop heavy "
                   f"jobs and retry.")
        eprint("If ffmpeg died with no message, look for an "
               "out-of-memory kill: "
               "journalctl --since \"24 hours ago\" "
               "| grep -i \"out of memory\"")
        return 1

    try:
        new_size = os.path.getsize(tmp_output)
        _pct = size_percent(new_size, filesize)
        _pct_s = f"{_pct}%" if _pct is not None else "?"
        dbg(f"done: {filesize} -> {new_size} bytes "
            f"({_pct_s} of source: 100 * {new_size} / {filesize})")
    except OSError:
        pass

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
