#!/usr/bin/env python3
"""Smart movie compression to AV1 (video) + Opus (audio) at minimal bitrate.

Use it through the mkv / mkvf wrappers:
    mkvf film.mkv      # "film" profile:     VMAF >= 94, SI-SDR >= 18 dB
    mkv  cartoon.mkv   # "animation" profile: VMAF >= 90, SI-SDR >= 15 dB

How it works:
    video: ab-av1 crf-search finds the highest CRF with VMAF >= target,
           then ab-av1 encode compresses the whole file
           (ab-av1 output goes to the screen as is);
    audio: for each track, a 60-second sample from the middle of the film
           is used to find the lowest Opus bitrate with SI-SDR >= target
           (ffmpeg asisdr filter, a VMAF-like score for audio).
           Bits above a lossy source bitrate are wasted, so the search
           ceiling is capped at the original bitrate.
           Opus that is already good enough is copied, not re-encoded.

The script itself stays quiet: one line per audio track, errors,
and one last line. Everything else on screen comes from ab-av1.
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

PRESET = 3
SAMPLE_SECS = 60  # audio sample length for bitrate search, in seconds
CACHE_DIR = "/tmp"

PROFILES = {
    # profile: (min_vmaf, min_sisdr_db)
    "film": (94, 18.0),       # mkvf
    "animation": (90, 15.0),  # mkv
}

# Candidate total Opus bitrates (per track), in kbit/s.
BITRATE_LADDER = [16, 24, 32, 48, 64, 80, 96, 128, 160, 192,
                  224, 256, 320, 384, 448, 512]

LOSSLESS_CODECS = {"flac", "alac", "mlp", "truehd",
                   "pcm_s16le", "pcm_s24le", "pcm_s32le",
                   "pcm_f32le", "pcm_f64le", "pcm_u8", "pcm_s16be",
                   "pcm_s24be", "pcm_s32be"}

# Fallback if sampling / measuring fails (table from the old bash version).
STATIC_FALLBACK = {1: 24, 2: 48, 6: 96, 7: 112, 8: 112}


def eprint(msg):
    print(msg, file=sys.stderr)


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
    hi = min(512, ch * 64)
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
        with tempfile.TemporaryDirectory(prefix="mkv_abrest_") as tmp:
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
    except Exception:
        return None


def ladder_for(lo, hi):
    """Standard ladder clipped to [lo, hi]; at least 2 steps."""
    cands = [b for b in BITRATE_LADDER if lo <= b <= hi]
    if not cands:
        cands = sorted({lo, hi})
    elif len(cands) == 1:
        cands = sorted({cands[0], hi} if hi != cands[0] else {cands[0], lo})
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


def pick_audio_bitrate(path, track, target_db, duration):
    """Lowest Opus bitrate for the target SI-SDR.

    Returns (bitrate_kbit, sisdr_db|None, method), where method is:
    copy — opus is already good enough, copy it; search — sample search;
    max — target not reached even at the ceiling; fallback — static table.
    """
    ch = track.get("channels") or 0
    codec = track.get("codec") or ""
    src_br = track.get("bit_rate")
    lo, hi = bitrate_bounds(track)

    if duration <= 0:
        duration = 120.0
    sample_dur = min(float(SAMPLE_SECS), max(10.0, duration * 0.05))
    start = max(0.0, duration * 0.33 - sample_dur / 2)

    # Bitrate unknown (normal for audio in MKV): guess it with a fast
    # sample copy, so a lossy source is never "upscaled".
    # Lossless guesses are always above the search ceiling, so no cap there.
    est_br = None
    if src_br is None and codec not in LOSSLESS_CODECS:
        est_br = estimate_src_bitrate(path, track["aindex"], start,
                                      sample_dur)
        if est_br:
            hi = min(hi, max(lo, math.ceil(est_br / 1000)))

    eff_br = src_br if src_br else est_br
    # Opus that is already good enough (bitrate at or below need) — copy it.
    if codec == "opus" and eff_br:
        if math.ceil(eff_br / 1000) <= hi:
            return None, None, "copy"

    if ch <= 0:
        return 64, None, "fallback"

    cands = ladder_for(lo, hi)

    try:
        with tempfile.TemporaryDirectory(prefix="mkv_audio_") as tmp:
            ref = os.path.join(tmp, "ref.wav")
            if not extract_sample(path, track["aindex"], start,
                                  sample_dur, ref,
                                  channels=track.get("channels") or 0):
                raise RuntimeError("sample")
            active = active_channels(ref)
            for br in cands:
                enc = os.path.join(tmp, f"enc_{br}.opus")
                if not encode_opus(ref, br, enc):
                    continue
                score = measure_sisdr(ref, enc, active)
                if score is None:
                    continue
                if score >= target_db:
                    if eff_br and eff_br <= br * 1000:
                        return None, None, "copy"
                    return br, round(score, 1), "search"
            # Target not reached. But if the source is already at the
            # ceiling, copy it: same bytes with no re-encode loss.
            if eff_br and eff_br <= cands[-1] * 1000:
                return None, None, "copy"
            # Else take the ceiling (closest to the target).
            enc = os.path.join(tmp, f"enc_{cands[-1]}.opus")
            score = None
            if encode_opus(ref, cands[-1], enc):
                score = measure_sisdr(ref, enc, active)
            return cands[-1], (round(score, 1) if score is not None
                               else None), "max"
    except Exception:
        pass
    fb = STATIC_FALLBACK.get(ch, 64)
    return fb, None, "fallback"


def run_crf_search(path, min_vmaf):
    """ab-av1 crf-search with live output. Returns the CRF (float)."""
    if not path or min_vmaf is None:
        return None
    cmd = ["ab-av1", "crf-search", "-i", path, "--preset", str(PRESET),
           "--min-vmaf", str(min_vmaf)]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
    except FileNotFoundError:
        return None
    found = None
    try:
        for line in p.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            m = re.findall(r"crf\s+(\d+(?:\.\d+)?)", line)
            if m:
                try:
                    found = float(m[-1])
                except ValueError:
                    pass
        p.wait()
    finally:
        try:
            p.stdout.close()
        except Exception:
            pass
    if p.returncode != 0:
        return None
    return found


def main():
    ap = argparse.ArgumentParser(
        description="Compress a movie: AV1 video (VMAF target) + Opus audio "
                    "(SI-SDR target), minimal bitrate.")
    ap.add_argument("input", nargs="?", help="input video file")
    ap.add_argument("--profile", choices=["film", "animation"],
                    default="film", help="quality profile")
    ap.add_argument("--min-vmaf", type=float, default=None)
    ap.add_argument("--min-sisdr", type=float, default=None,
                    help="target audio SI-SDR, dB")
    args = ap.parse_args()

    if not args.input:
        eprint(f"Usage: {os.path.basename(sys.argv[0])} input.mkv")
        return 1
    if not os.path.isfile(args.input):
        eprint(f"File not found: {args.input}")
        return 1

    prof_vmaf, prof_sisdr = PROFILES[args.profile]
    min_vmaf = args.min_vmaf if args.min_vmaf is not None else prof_vmaf
    min_sisdr = args.min_sisdr if args.min_sisdr is not None else prof_sisdr

    abs_input = os.path.realpath(args.input)
    filename = os.path.basename(abs_input)
    try:
        filesize = os.path.getsize(abs_input)
    except OSError as e:
        eprint(f"Cannot read file: {e}")
        return 1

    cache_id = f"{filename}.{filesize}b.vmaf{min_vmaf}.sisdr{min_sisdr}"
    cache_path = os.path.join(CACHE_DIR, cache_id + ".json")
    # Cache from the old bash version (CRF only, no audio target) is reused.
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
            eprint("Cancelled by user.")
            return 1
        if ans not in ("y", "yes"):
            eprint("Cancelled by user.")
            return 1

    # --- cache ---
    crf = None
    audio_plan = None  # list of (bitrate_k|None, method) per aindex
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            crf = float(cached.get("crf"))
            audio_plan = [(a[0], a[1]) for a in cached.get("audio", [])]
        except (OSError, ValueError, TypeError, KeyError):
            crf, audio_plan = None, None

    duration, tracks = probe(abs_input)

    # Cache from other tracks (same name/size, other streams) —
    # the audio plan is useless, search again.
    if audio_plan is not None and len(audio_plan) != len(tracks):
        audio_plan = None

    # --- 1. video: CRF ---
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
                with open(cache_path + ".tmp", "w",
                          encoding="utf-8") as f:
                    json.dump({"crf": crf, "audio": []}, f)
                os.replace(cache_path + ".tmp", cache_path)
            except OSError:
                pass
            # legacy file, for old wrappers
            try:
                with open(legacy_crf, "w", encoding="utf-8") as f:
                    f.write(str(crf))
            except OSError:
                pass

    # --- 2. audio: bitrates ---
    if audio_plan is None:
        audio_plan = []
        for t in tracks:
            br, score, method = pick_audio_bitrate(abs_input, t, min_sisdr,
                                                   duration)
            audio_plan.append((br, method))
            ch_txt = str(t["channels"]) if t["channels"] else "?"
            if method == "copy":
                eprint(f"audio {t['aindex']}: {t['lang']} {ch_txt}ch "
                       f"{t['codec']} — already good enough, copying")
            elif method == "search":
                eprint(f"audio {t['aindex']}: {t['lang']} {ch_txt}ch → "
                       f"opus {br}k (SI-SDR {score} dB)")
            elif method == "max":
                extra = f" (SI-SDR {score} dB)" if score is not None else ""
                eprint(f"audio {t['aindex']}: {t['lang']} {ch_txt}ch → "
                       f"opus {br}k — target {min_sisdr} dB not reached"
                       f"{extra}, using ceiling")
            else:
                eprint(f"audio {t['aindex']}: {t['lang']} {ch_txt}ch → "
                       f"opus {br}k (measure failed)")
        try:
            with open(cache_path + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"crf": crf, "audio": audio_plan}, f)
            os.replace(cache_path + ".tmp", cache_path)
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
        rc = subprocess.run(cmd).returncode
    except FileNotFoundError:
        eprint("ab-av1 not found. Install ab-av1 and ffmpeg.")
        return 1
    except KeyboardInterrupt:
        try:
            if os.path.isfile(tmp_output):
                os.remove(tmp_output)
        except OSError:
            pass
        eprint("Stopped by user. Input file untouched.")
        return 130

    if rc != 0:
        try:
            if os.path.isfile(tmp_output):
                os.remove(tmp_output)
        except OSError:
            pass
        eprint("Encode failed! Input file untouched.")
        return 1

    try:
        new_size = os.path.getsize(tmp_output)
    except OSError:
        new_size = -1
    try:
        os.rename(abs_input, bak_file)
        os.rename(tmp_output, abs_input)
    except OSError as e:
        eprint(f"Error replacing files: {e}")
        return 1
    if new_size >= 0:
        eprint(f"Done: {filename}: {filesize / 1048576:.0f} → "
               f"{new_size / 1048576:.0f} MB (original: {bak_file}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
