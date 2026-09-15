"""Tests for mkv_encode.py. Run: python3 -m pytest tests/"""
import json
import math
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mkv_encode as m


def _res(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode,
                                 stdout=stdout, stderr=stderr)


# --- eprint / run_quiet / sdr_to_sisdr ---

def test_eprint_goes_to_stderr(capsys):
    m.eprint("hello")
    assert capsys.readouterr().err.strip() == "hello"


def test_run_quiet_captures():
    r = m.run_quiet(["echo", "hi"])
    assert r.returncode == 0 and r.stdout.strip() == "hi"


def test_sdr_to_sisdr():
    import pytest
    assert m.sdr_to_sisdr(0) == 0.0
    assert m.sdr_to_sisdr(100) == m.SISDR_AT_100
    assert m.sdr_to_sisdr(72) == pytest.approx(18.0)
    assert m.sdr_to_sisdr(60) == pytest.approx(15.0)
    assert m.sdr_to_sisdr(32) == pytest.approx(8.0)


def test_parse_denoise():
    # --noise takes no arguments: flag on/off only.
    assert m.parse_denoise(None) is None
    assert m.parse_denoise(False) is None
    assert m.parse_denoise(True) == "auto"
    assert m.parse_denoise("auto") == "auto"
    for bad in ("", "svt", "hqdn3d", "off", "nlmeans", 5):
        try:
            m.parse_denoise(bad)
            assert False, f"must reject {bad!r}"
        except ValueError:
            pass


def test_svt_args_for_denoise():
    assert m.svt_args_for_denoise(None) == []
    args = m.svt_args_for_denoise("hqdn3d")
    assert f"film-grain={m.SVT_FILM_GRAIN_DEFAULT}" in args
    assert "film-grain-denoise=0" in args


def test_parse_denoise_off_and_levels():
    assert m.denoise_algo("removegrain") == "removegrain"
    assert m.denoise_algo("fftdnoiz:12") == "fftdnoiz"
    assert m.denoise_grain_level(None) is None
    assert m.denoise_grain_level("hqdn3d") == m.SVT_FILM_GRAIN_DEFAULT
    assert m.denoise_grain_level("removegrain:12") == 12
    import pytest
    for bad in ("hqdn3d:0", "hqdn3d:51", "removegrain:x"):
        try:
            m.parse_denoise(bad)
            assert False, f"must reject {bad}"
        except ValueError:
            pass


def test_parse_denoise_algos():
    for bad in ("hqdn3d", "hqdn3d:12", "removegrain", "off", ""):
        try:
            m.parse_denoise(bad)
            assert False, f"flag takes no value, must reject {bad!r}"
        except ValueError:
            pass
    assert m.denoise_algo("hqdn3d-strong:12") == "hqdn3d-strong"
    assert m.denoise_algo("fftdnoiz:12") == "fftdnoiz"
    assert m.denoise_algo("atadenoise") == "atadenoise"
    assert m.denoise_algo("removegrain") == "removegrain"
    assert m.denoise_algo("fftdnoiz:4") == "fftdnoiz"
    assert m.denoise_algo("svt") is None
    assert m.denoise_algo(None) is None
    assert m.denoise_grain_level("hqdn3d:12") == 12
    import pytest
    for bad in ("hqdn3d:0", "hqdn3d:51", "atadenoise:x", "nlmeans"):
        try:
            m.parse_denoise(bad)
            assert False, f"must reject {bad}"
        except ValueError:
            pass


def test_svt_args_filter_modes():
    assert m.svt_args_for_denoise("hqdn3d") == [
        "film-grain=8", "film-grain-denoise=0"]
    assert m.svt_args_for_denoise("atadenoise:12") == [
        "film-grain=12", "film-grain-denoise=0"]
    assert m.svt_args_for_denoise("removegrain") == [
        "film-grain=8", "film-grain-denoise=0"]
    assert m.vfilter_for_denoise(None) is None
    try:
        m.vfilter_for_denoise("svt")
        assert False, "svt removed"
    except ValueError:
        pass
    assert m.vfilter_for_denoise("hqdn3d") == m.HQDN3D_DEFAULT
    assert "hqdn3d=" in m.vfilter_for_denoise("hqdn3d:12")
    assert m.vfilter_for_denoise("atadenoise") == m.ATADENOISE_DEFAULT
    assert m.vfilter_for_denoise("removegrain") == m.FILTER_CANDIDATES["removegrain"]
    assert m.vfilter_for_denoise("fftdnoiz") == m.FILTER_CANDIDATES["fftdnoiz"]
    import pytest
    try:
        m.vfilter_for_denoise("nlmeans")
        assert False, "must reject nlmeans"
    except ValueError:
        pass


def test_make_cache_id_filter_modes():
    base = m.make_cache_id("f.mkv", 100, 94, 72)
    assert m.make_cache_id("f.mkv", 100, 94, 72,
                           denoise="hqdn3d") == base + ".noisehqdn3d8"
    assert m.make_cache_id("f.mkv", 100, 94, 72,
                           denoise="hqdn3d:12") == base + ".noisehqdn3d12"
    assert m.make_cache_id("f.mkv", 100, 94, 72,
                           denoise="atadenoise") == base + ".noiseatadenoise8"
    assert m.make_cache_id("f.mkv", 100, 94, 72,
                           denoise="hqdn3d") != base + ".noisefftdnoiz8"
    assert m.make_cache_id("f.mkv", 100, 94, 72,
                           denoise="removegrain") == base + ".noiseremovegrain8"
    assert m.make_cache_id("f.mkv", 100, 94, 72,
                           denoise="fftdnoiz:12") == base + ".noisefftdnoiz12"


def test_svt_args_levels():
    assert m.svt_args_for_denoise("hqdn3d:4") == [
        "film-grain=4", "film-grain-denoise=0"]
    assert m.svt_args_for_denoise("off") == []
    assert m.svt_args_for_denoise("none") == []


def test_make_cache_id_modes():
    base = m.make_cache_id("f.mkv", 100, 94, 72)
    assert base == "f.mkv.100b.vmaf94.sdr72"
    # deprecated bool alias maps to the hqdn3d default
    assert m.make_cache_id("f.mkv", 100, 94, 72,
                           noise=True) == base + ".noisehqdn3d8"
    assert m.make_cache_id("f.mkv", 100, 94, 72,
                           denoise="hqdn3d") == base + ".noisehqdn3d8"
    assert m.make_cache_id("f.mkv", 100, 94, 72, denoise=None) == base
    assert m.make_cache_id("f.mkv", 100, 94, 72, denoise="hqdn3d:4") != \
        m.make_cache_id("f.mkv", 100, 94, 72, denoise="hqdn3d:12")


def test_format_audio_result():
    assert m.format_audio_result(None, "copy", None, 100) == "- copy (100%)"
    assert m.format_audio_result(None, "copy", None, 100,
                                 2) == "- sound 3: copy (100%)"
    assert m.format_audio_result(48, "search", 18.0, 38,
                                 0) == "- sound 1: vbr 48 SI-SDR 72 (38%)"
    r = m.format_audio_result(48, "search", 18.0, 38)
    assert r.startswith("- vbr 48") and "(38%)" in r and "SI-SDR" in r
    r2 = m.format_audio_result(48, "search", None, None)
    assert r2 == "- vbr 48"


def test_parse_progress_seconds():
    assert m._parse_progress_seconds("out_time_ms=12345678") == 12.345678
    assert m._parse_progress_seconds("out_time=00:01:02.50") == 62.5
    assert m._parse_progress_seconds("frame=10") is None
    assert m._parse_progress_seconds("") is None
    assert m._parse_progress_seconds(None) is None
    assert m._parse_progress_seconds("out_time_ms=bogus") is None


def test_fmt_elapsed():
    assert m._fmt_elapsed(0) == "00:00:00"
    assert m._fmt_elapsed(12) == "00:00:12"
    assert m._fmt_elapsed(3723) == "01:02:03"
    assert m._fmt_elapsed(None) == "00:00:00"
    assert m._fmt_elapsed(-5) == "00:00:00"


def test_strip_ansi():
    assert m._strip_ansi("\x1b[36mhi\x1b[0m") == "hi"
    assert m._strip_ansi("plain") == "plain"


def test_sisdr_roundtrip():
    assert m.sdr_to_sisdr(100) == m.SISDR_AT_100
    assert abs(m.sisdr_to_sdr(m.sdr_to_sisdr(72)) - 72) < 1e-9


def test_is_abav1_dir_name():
    assert m._is_abav1_dir_name(".ab-av1-x") is True
    assert m._is_abav1_dir_name("film.mkv") is False
    assert m._is_abav1_dir_name(None) is False


def test_video_size(monkeypatch, tmp_path):
    film = tmp_path / "v.mkv"
    film.write_bytes(b"0")
    payload = '{"streams": [{"codec_type": "video", "width": 1920, "height": 800}]}'
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(0, payload, ""))
    assert m.video_size(str(film)) == (1920, 800)
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(1, "", "e"))
    assert m.video_size(str(film)) == (None, None)
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(0, "{oops", ""))
    assert m.video_size(str(film)) == (None, None)
    assert m.video_size("/no/file.mkv") == (None, None)


def test_detect_crop_consensus(monkeypatch, tmp_path):
    film = tmp_path / "c.mkv"
    film.write_bytes(b"0" * 1024)
    monkeypatch.setattr(m, "video_size", lambda _p: (1920, 1080))
    err = "[Parsed_cropdetect_0] crop=1920:800:0:140\n" * 200
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(0, "", err))
    assert m.detect_crop(str(film), 6000.0) == "1920:800:0:140"


def test_detect_crop_no_consensus(monkeypatch, tmp_path):
    film = tmp_path / "c.mkv"
    film.write_bytes(b"0" * 1024)
    monkeypatch.setattr(m, "video_size", lambda _p: (1920, 1080))
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(0, "", "nothing"))
    assert m.detect_crop(str(film), 6000.0) is None
    assert m.detect_crop("/no/file.mkv", 6000.0) is None
    assert m.detect_crop(str(film), 0) is None


def test_ensure_abav1_temp_reuses(monkeypatch, tmp_path):
    monkeypatch.setattr(m.tempfile, "mkdtemp",
                        lambda prefix="": str(tmp_path))
    m._ABAV1_TEMP = None
    first = m.ensure_abav1_temp()
    assert first == str(tmp_path)
    monkeypatch.setattr(m.os.path, "isdir", lambda _p: True)
    assert m.ensure_abav1_temp() == str(tmp_path)
    m._ABAV1_TEMP = None
    m._CLEANUP_DIRS.discard(str(tmp_path))


def test_kill_children_terminates(monkeypatch):
    seen = []

    class _P:
        pid = 4242

        def wait(self, timeout=None):
            seen.append(("wait", timeout))
            return 0

    monkeypatch.setattr(m.os, "killpg",
                        lambda pid, sig: seen.append(("killpg", pid, sig)))
    m._CHILD_PROCS.clear()
    m._CHILD_PROCS.append(_P())
    m._kill_children()
    assert ("killpg", 4242, m.signal.SIGTERM) in seen
    assert m._CHILD_PROCS == []


def test_on_signal_cleans_and_exits(monkeypatch):
    import pytest as _pytest
    monkeypatch.setattr(m, "cleanup_generated", lambda *a, **k: None)
    with _pytest.raises(SystemExit) as _exc:
        m._on_signal(m.signal.SIGTERM, None)
    assert _exc.value.code == 128 + m.signal.SIGTERM


def test_dbg_only_with_flag(monkeypatch, capsys):
    monkeypatch.setattr(m, "DEBUG", False)
    m.dbg("quiet")
    assert capsys.readouterr().err == ""
    monkeypatch.setattr(m, "DEBUG", True)
    m.dbg("loud")
    assert "loud" in capsys.readouterr().err
    monkeypatch.setattr(m, "DEBUG", False)


def test_fmt_eta():
    assert m._fmt_eta(None) == "eta ?"
    assert m._fmt_eta(-5) == "eta ?"
    assert m._fmt_eta(0) == "eta 0s"
    assert m._fmt_eta(42) == "eta 42s"
    assert m._fmt_eta(130) == "eta 2m"
    assert m._fmt_eta(4000) == "eta 1h"
    assert m._spinner_eta(0, 27, 5) is None
    assert m._spinner_eta(27, 27, 60) == 0.0
    assert abs(m._spinner_eta(13, 27, 60.0) - 64.6) < 0.1
    assert m._spinner_eta(0, 0, 5) is None


def test_parse_cropdetect_line():
    assert m.parse_cropdetect_line(
        "[Parsed_cropdetect_0] crop=1920:800:0:140") == (1920, 800, 0, 140)
    assert m.parse_cropdetect_line("no crop here") is None
    assert m.parse_cropdetect_line("") is None
    assert m.parse_cropdetect_line(None) is None
    assert m.parse_cropdetect_line("crop=0:0:0:0") is None


def test_pick_crop():
    counts = {(1920, 800, 0, 140): 3, (1920, 816, 0, 132): 1}
    assert m.pick_crop(counts, 1920, 1080) == "1920:800:0:140"
    assert m.pick_crop({(1920, 800, 0, 140): 1}, 1920, 1080) is None
    assert m.pick_crop({}, 1920, 1080) is None
    assert m.pick_crop(counts, None, 1080) is None
    # tie goes to the smaller area
    tied = {(1920, 800, 0, 140): 2, (1920, 816, 0, 132): 2}
    assert m.pick_crop(tied, 1920, 1080) == "1920:800:0:140"
    # outside the frame or tiny area is rejected
    assert m.pick_crop({(2000, 800, 0, 140): 3}, 1920, 1080) is None
    assert m.pick_crop({(640, 360, 0, 0): 3}, 1920, 1080) is None
    assert m.pick_crop({(1921, 800, 0, 140): 3}, 1920, 1080) is None


def test_make_cache_id_crop():
    base = m.make_cache_id("f.mkv", 100, 94, 72)
    assert m.make_cache_id("f.mkv", 100, 94, 72,
                           crop="1920:800:0:140") == base + ".crop1920x800"
    assert m.make_cache_id("f.mkv", 100, 94, 72, denoise="hqdn3d",
                           crop="1920:800:0:140") == \
        base + ".noisehqdn3d8.crop1920x800"
    assert m.make_cache_id("f.mkv", 100, 94, 72, crop="bogus") == base


def test_nothing_to_compress():
    assert m.nothing_to_compress(True, [(None, "copy", None, 100)]) is True
    assert m.nothing_to_compress(False, [(None, "copy", None, 100)]) is False
    assert m.nothing_to_compress(
        True, [(48, "search", 18.0, 38)]) is False


# --- probe ---

FFPROBE_JSON = json.dumps({
    "format": {"duration": "120.5"},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "AAC",
         "channels": 2, "bit_rate": "128000",
         "tags": {"language": "eng"}},
        {"index": 2, "codec_type": "audio", "codec_name": "AC3",
         "channels": 6, "bit_rate": "N/A", "tags": {}},
    ],
})


def test_probe_parses_tracks(monkeypatch, tmp_path):
    fake = str(tmp_path / "film.mkv")
    open(fake, "wb").write(b"0")
    monkeypatch.setattr(m, "run_quiet",
                        lambda cmd: _res(0, FFPROBE_JSON, ""))
    dur, tracks = m.probe(fake)
    assert dur == 120.5
    assert len(tracks) == 2
    assert tracks[0]["aindex"] == 0
    assert tracks[0]["channels"] == 2
    assert tracks[0]["codec"] == "aac"  # lowered
    assert tracks[0]["bit_rate"] == 128000
    assert tracks[0]["lang"] == "eng"
    assert tracks[1]["aindex"] == 1  # audio-only numbering
    assert tracks[1]["bit_rate"] is None  # "N/A"
    assert tracks[1]["lang"] == "?"


def test_probe_missing_file():
    assert m.probe("/no/such/file.mkv") == (0.0, [])


def test_probe_ffprobe_error(monkeypatch, tmp_path):
    fake = str(tmp_path / "film.mkv")
    open(fake, "wb").write(b"0")
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(1, "", "boom"))
    assert m.probe(fake) == (0.0, [])


def test_probe_bad_json(monkeypatch, tmp_path):
    fake = str(tmp_path / "film.mkv")
    open(fake, "wb").write(b"0")
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(0, "{oops", ""))
    assert m.probe(fake) == (0.0, [])


# --- bitrate_bounds ---

def test_bounds_mono():
    assert m.bitrate_bounds({"channels": 1}) == (16, 96)


def test_bounds_stereo():
    assert m.bitrate_bounds({"channels": 2}) == (24, 192)


def test_bounds_51():
    assert m.bitrate_bounds({"channels": 6}) == (72, 512)


def test_bounds_unknown_channels():
    assert m.bitrate_bounds({"channels": 0}) == (32, 128)


def test_bounds_lossy_capped():
    lo, hi = m.bitrate_bounds({"channels": 2, "codec": "aac",
                               "bit_rate": 96000})
    assert (lo, hi) == (24, 96)


def test_bounds_lossless_not_capped():
    assert m.bitrate_bounds({"channels": 2, "codec": "flac",
                             "bit_rate": 900000}) == (24, 192)


def test_bounds_opus_not_capped():
    assert m.bitrate_bounds({"channels": 2, "codec": "opus",
                             "bit_rate": 400000}) == (24, 192)


# --- ladder_for ---

def test_ladder_normal():
    assert m.ladder_for(24, 128) == [24, 32, 40, 48, 56, 64, 72, 80, 96,
                                     112, 128]


def test_ladder_empty_range():
    assert m.ladder_for(1000, 2000) == [1000, 2000]


def test_ladder_single_step_gets_two():
    got = m.ladder_for(320, 330)
    assert len(got) == 2 and got[0] == 320


def test_ladder_equal_bounds_get_two():
    got = m.ladder_for(64, 64)
    assert len(got) == 2 and 64 in got


# --- estimate_src_bitrate ---

def test_estimate_guards():
    assert m.estimate_src_bitrate(None, 0, 0.0, 10.0) is None
    assert m.estimate_src_bitrate("f", 0, 0.0, 0) is None


def test_estimate_measures_copy(monkeypatch, tmp_path):
    sample = tmp_path / "src.mka"
    sample.write_bytes(b"x" * 16000)  # 16 kB over 10 s -> 12800 bps
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(0, "", ""))

    def fake_mkdtemp(prefix=""):
        return str(tmp_path)

    monkeypatch.setattr(m.tempfile, "mkdtemp", fake_mkdtemp)
    # avoid deleting the test tmp_path in finally
    monkeypatch.setattr(m.shutil, "rmtree", lambda *a, **k: None)
    got = m.estimate_src_bitrate("film.mkv", 0, 0.0, 10.0)
    assert got == 16000 * 8 // 10


def test_estimate_copy_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(1, "", "err"))
    monkeypatch.setattr(m.tempfile, "mkdtemp", lambda prefix="": str(tmp_path))
    monkeypatch.setattr(m.shutil, "rmtree", lambda *a, **k: None)
    assert m.estimate_src_bitrate("film.mkv", 0, 0.0, 10.0) is None


# --- extract_sample / encode_opus ---

def test_extract_sample_adds_channelmap_for_51(monkeypatch, tmp_path):
    seen = {}
    out = str(tmp_path / "ref.wav")
    open(out, "wb").write(b"1234")

    def fake_run(cmd):
        seen["cmd"] = cmd
        return _res(0, "", "")
    monkeypatch.setattr(m, "run_quiet", fake_run)
    assert m.extract_sample("f.mkv", 1, 5.0, 10.0, out, channels=6) is True
    assert "-af" in seen["cmd"] and "channelmap=channel_layout=5.1" in seen["cmd"]


def test_extract_sample_no_channelmap_for_stereo(monkeypatch, tmp_path):
    seen = {}
    out = str(tmp_path / "ref.wav")
    open(out, "wb").write(b"1234")
    monkeypatch.setattr(m, "run_quiet",
                        lambda cmd: (seen.update(cmd=cmd), _res(0, "", ""))[1])
    assert m.extract_sample("f.mkv", 0, 0.0, 10.0, out, channels=2) is True
    assert "-af" not in seen["cmd"]


def test_extract_sample_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(1, "", "err"))
    assert m.extract_sample("f", 0, 0, 1, str(tmp_path / "x.wav")) is False


def test_encode_opus_ok_and_fail(monkeypatch, tmp_path):
    out = str(tmp_path / "e.opus")
    open(out, "wb").write(b"1")
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(0, "", ""))
    assert m.encode_opus("ref.wav", 64, out) is True
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(1, "", "e"))
    assert m.encode_opus("ref.wav", 64, out) is False


# --- active_channels ---

ASTATS = """
[Parsed_astats_0 @ 0x1] Channel: 1
[Parsed_astats_0 @ 0x1] Peak level dB: -13.4
[Parsed_astats_0 @ 0x1] RMS level dB: -22.8
[Parsed_astats_0 @ 0x1] Channel: 2
[Parsed_astats_0 @ 0x1] Peak level dB: -13.4
[Parsed_astats_0 @ 0x1] RMS level dB: -22.8
[Parsed_astats_0 @ 0x1] Channel: 3
[Parsed_astats_0 @ 0x1] Peak level dB: -90.3
[Parsed_astats_0 @ 0x1] RMS level dB: -117.0
[Parsed_astats_0 @ 0x1] Channel: 4
[Parsed_astats_0 @ 0x1] Peak level dB: -inf
[Parsed_astats_0 @ 0x1] RMS level dB: -inf
[Parsed_astats_0 @ 0x1] Peak level dB: -13.4
[Parsed_astats_0 @ 0x1] RMS level dB: -27.6
"""


def test_active_channels_skips_quiet(monkeypatch, tmp_path):
    ref = str(tmp_path / "r.wav")
    open(ref, "wb").write(b"1")
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(0, "", ASTATS))
    assert m.active_channels(ref) == [0, 1]


def test_active_channels_missing_file():
    assert m.active_channels("/no/file.wav") is None


def test_active_channels_no_data(monkeypatch, tmp_path):
    ref = str(tmp_path / "r.wav")
    open(ref, "wb").write(b"1")
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(0, "", "nothing"))
    assert m.active_channels(ref) is None


# --- measure_sisdr ---

SISDR_2CH = ("[Parsed_asisdr_0 @ 0x1] SI-SDR ch0: 18.5 dB\n"
             "[Parsed_asisdr_0 @ 0x1] SI-SDR ch1: 17.5 dB\n")


def _files(tmp_path):
    a = str(tmp_path / "r.wav")
    b = str(tmp_path / "e.opus")
    open(a, "wb").write(b"1")
    open(b, "wb").write(b"1")
    return a, b


def test_measure_average(monkeypatch, tmp_path):
    a, b = _files(tmp_path)
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(0, "", SISDR_2CH))
    assert m.measure_sisdr(a, b) == 18.0


def test_measure_active_gating(monkeypatch, tmp_path):
    a, b = _files(tmp_path)
    err = ("SI-SDR ch0: 20.0 dB\nSI-SDR ch1: -30.0 dB\n"
           "SI-SDR ch2: 16.0 dB\nSI-SDR ch3: -nan dB\n")
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(0, "", err))
    assert m.measure_sisdr(a, b, active=[0, 2]) == 18.0


def test_measure_silence_is_inf(monkeypatch, tmp_path):
    a, b = _files(tmp_path)
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(0, "", SISDR_2CH))
    assert m.measure_sisdr(a, b, active=[]) == float("inf")


def test_measure_no_values(monkeypatch, tmp_path):
    a, b = _files(tmp_path)
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(0, "", "nope"))
    assert m.measure_sisdr(a, b) is None


def test_measure_missing_files():
    assert m.measure_sisdr("/no/a.wav", "/no/b.opus") is None


# --- _json_safe_score ---

def test_json_safe_score():
    assert m._json_safe_score(18.34) == 18.3
    assert m._json_safe_score(None) is None
    assert m._json_safe_score(float("inf")) is None
    assert m._json_safe_score(float("nan")) is None


# --- load_cache ---

def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_load_cache_good(tmp_path):
    p = str(tmp_path / "c.json")
    _write(p, '{"crf": 55.5, "audio": [[80, "search", 18.3, 62], [null, "copy", null, 100]]}')
    assert m.load_cache(p) == (
        55.5, [(80, "search", 18.3, 62), (None, "copy", None, 100)], None)


def test_load_cache_audio_only(tmp_path):
    p = str(tmp_path / "c.json")
    _write(p, '{"crf": null, "audio": [[48, "search", 18.0, 38]]}')
    assert m.load_cache(p) == (None, [(48, "search", 18.0, 38)], None)


def test_load_cache_legacy_two_fields(tmp_path):
    # older caches store (br, method); score/pct are unknown
    p = str(tmp_path / "c.json")
    _write(p, '{"crf": 55.5, "audio": [[80, "search"], [null, "copy"]]}')
    crf, plan, video = m.load_cache(p)
    assert crf == 55.5
    assert plan == [(80, "search", None, None), (None, "copy", None, None)]
    assert video is None


def test_load_cache_video_copy(tmp_path):
    p = str(tmp_path / "c.json")
    _write(p, '{"crf": null, "audio": [], "video": "copy"}')
    assert m.load_cache(p) == (None, [], "copy")


def test_load_cache_missing():
    assert m.load_cache("/no/cache.json") == (None, None, None)


def test_load_cache_garbage(tmp_path):
    p = str(tmp_path / "c.json")
    _write(p, "{oops")
    assert m.load_cache(p) == (None, None, None)


def test_load_cache_bad_shapes(tmp_path):
    for bad_audio in ['"ab"', '{"x": 1}', '5', '[["a"]]', '[[80]]',
                      '[[80, "nope"]]', '[["80", "search"]]', "[[80]]",
                      '[[80, "search", "bad", 10]]']:
        p = str(tmp_path / "c.json")
        _write(p, '{"crf": 55, "audio": %s}' % bad_audio)
        assert m.load_cache(p) == (None, None, None), bad_audio


def test_load_cache_bool_crf_rejected(tmp_path):
    p = str(tmp_path / "c.json")
    _write(p, '{"crf": true, "audio": []}')
    assert m.load_cache(p) == (None, None, None)


def test_load_cache_bad_crf(tmp_path):
    p = str(tmp_path / "c.json")
    _write(p, '{"crf": "abc", "audio": []}')
    assert m.load_cache(p) == (None, None, None)


# --- pick_audio_bitrate ---

def _track(**kw):
    t = {"aindex": 0, "channels": 2, "codec": "aac",
         "bit_rate": 128000, "lang": "eng"}
    t.update(kw)
    return t


def _asisdr_ok(monkeypatch, ok=True):
    rc = 0 if ok else 1
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(rc, "", ""))


def test_pick_opus_copy(monkeypatch):
    _asisdr_ok(monkeypatch)
    assert m.pick_audio_bitrate("f", _track(codec="opus", bit_rate=48000),
                                18.0, 100.0) == (None, "copy", None, 100)


def test_pick_no_meter_copy(monkeypatch):
    _asisdr_ok(monkeypatch, ok=False)
    assert m.pick_audio_bitrate("f", _track(), 18.0,
                                100.0) == (None, "copy", None, 100)


def test_pick_unknown_channels_copy(monkeypatch):
    _asisdr_ok(monkeypatch)
    assert m.pick_audio_bitrate(
        "f", _track(channels=0), 18.0, 100.0) == (None, "copy", None, 100)


def _mock_search(monkeypatch, scores, tmp_path=None):
    """scores: {bitrate_k: sisdr} for the fake ladder walk."""
    _asisdr_ok(monkeypatch)
    monkeypatch.setattr(m, "extract_sample",
                        lambda *a, **k: True)
    monkeypatch.setattr(m, "active_channels", lambda ref: [0])
    monkeypatch.setattr(m, "encode_opus",
                        lambda ref, br, out: True)

    def fake_measure(ref, enc, active):
        import re
        mm = re.search(r"enc_(\d+)", enc)
        return scores.get(int(mm.group(1)), 99.0)
    monkeypatch.setattr(m, "measure_sisdr", fake_measure)
    if tmp_path is not None:
        monkeypatch.setattr(m.tempfile, "mkdtemp",
                            lambda prefix="": str(tmp_path))
        monkeypatch.setattr(m.shutil, "rmtree", lambda *a, **k: None)


def test_pick_search_lowest_ok(monkeypatch, tmp_path):
    # stereo ladder crosses target 15 between 40 and 48k:
    # interpolation gives the exact 42k, verified by one encode
    # (33% of 128k source), not the coarse ladder step.
    _mock_search(monkeypatch,
                 {24: 10.0, 32: 14.0, 40: 14.5, 42: 15.2, 48: 18.3},
                 tmp_path)
    assert m.pick_audio_bitrate("f", _track(), 15.0,
                                100.0) == (42, "search", 15.2, 33)


def test_pick_exact_interpolated(monkeypatch, tmp_path):
    # crossing 16 dB between 40 (14.0) and 48 (18.0) predicts 44k;
    # the verify encode hits, so the pick is exact, not 48k.
    _mock_search(monkeypatch,
                 {24: 10.0, 32: 12.0, 40: 14.0, 44: 16.1, 48: 18.0},
                 tmp_path)
    assert m.pick_audio_bitrate("f", _track(), 16.0,
                                100.0) == (44, "search", 16.1, 34)


def test_pick_exact_verify_fallback(monkeypatch, tmp_path):
    # predicted 44k misses (15.5 < 16): fall back to the ladder hit.
    _mock_search(monkeypatch,
                 {24: 10.0, 32: 12.0, 40: 14.0, 44: 15.5, 48: 18.0},
                 tmp_path)
    assert m.pick_audio_bitrate("f", _track(), 16.0,
                                100.0) == (48, "search", 18.0, 38)


def test_interp_br():
    assert m.interp_br(40, 14.0, 48, 18.0, 16.0) == 44
    assert m.interp_br(40, 14.5, 48, 18.3, 15.0) == 42
    assert m.interp_br(72, 16.0, 80, 18.0, 18.0) is None  # already on ladder
    assert m.interp_br(24, 10.0, 32, 18.3, 15.0) == 29
    assert m.interp_br(48, 18.0, 56, 19.0, 16.0) is None
    assert m.interp_br(40, 18.0, 48, 18.0, 16.0) is None
    assert m.interp_br(40, None, 48, 18.0, 16.0) is None
    assert m.interp_br("x", 14.0, 48, 18.0, 16.0) is None


def test_pick_search_copy_when_source_lean(monkeypatch, tmp_path):
    # source 32k aac, target met only at the 32k ceiling with margin:
    # same bytes -> copy, no re-encode loss
    _mock_search(monkeypatch, {24: 10.0, 32: 18.3}, tmp_path)
    assert m.pick_audio_bitrate("f", _track(bit_rate=32000), 15.0,
                                100.0) == (None, "copy", None, 100)


def test_pick_unreachable_target_copy(monkeypatch, tmp_path):
    # dense content, target out of reach: saturation finds nothing
    # sensible at 44 dB bound -> copy instead of blind ceiling
    _mock_search(monkeypatch, {}, tmp_path)
    monkeypatch.setattr(m, "measure_sisdr",
                        lambda ref, enc, active: 0.0)
    assert m.pick_audio_bitrate(
        "f", _track(codec="flac", bit_rate=900000), 50.0,
        100.0) == (None, "copy", None, 100)


def test_pick_unreachable_prefers_copy(monkeypatch, tmp_path):
    _mock_search(monkeypatch, {}, tmp_path)
    monkeypatch.setattr(m, "measure_sisdr",
                        lambda ref, enc, active: 0.0)
    # 64k aac capped at 64k ceiling: same bytes -> copy
    assert m.pick_audio_bitrate("f", _track(bit_rate=64000), 50.0,
                                100.0) == (None, "copy", None, 100)


def test_pick_estimate_caps_unknown(monkeypatch, tmp_path):
    _asisdr_ok(monkeypatch)
    monkeypatch.setattr(m, "estimate_src_bitrate",
                        lambda *a: 64000)
    monkeypatch.setattr(m, "extract_sample", lambda *a, **k: True)
    monkeypatch.setattr(m, "active_channels", lambda ref: [0])
    monkeypatch.setattr(m, "encode_opus", lambda ref, br, out: True)
    monkeypatch.setattr(m, "measure_sisdr",
                        lambda ref, enc, active: 0.0)
    monkeypatch.setattr(m.tempfile, "mkdtemp",
                        lambda prefix="": str(tmp_path))
    monkeypatch.setattr(m.shutil, "rmtree", lambda *a, **k: None)
    # unknown-bitrate aac guessed at 64k, target unreachable -> copy
    assert m.pick_audio_bitrate("f", _track(bit_rate=None), 50.0,
                                100.0) == (None, "copy", None, 100)


def _mock_ranked_search(monkeypatch, tmp_path, hard_ri, easy_score,
                          hard_curve):
    """Rank refs rank_{ri}.wav: hard_ri places follow hard_curve {br: s}."""
    _asisdr_ok(monkeypatch)
    calls = {"encode": 0, "refine_extracts": []}
    monkeypatch.setattr(m, "extract_sample",
                        lambda *a, **k: True)
    monkeypatch.setattr(m, "active_channels", lambda ref: [0])

    def fake_encode(ref, br, out):
        calls["encode"] += 1
        return True

    def fake_measure(ref, enc, active):
        import re
        mm = re.search(r"enc_(\d+)", enc)
        br = int(mm.group(1)) if mm else 0
        mh = re.search(r"rank_(\d+)\.wav", ref)
        if mh is not None:
            return (hard_curve.get(br, 99.0) if int(mh.group(1)) in hard_ri
                    else easy_score)
        mr = re.search(r"refine_(\d+)\.wav", ref)
        if mr is not None:
            calls["refine_extracts"].append(ref)
            return hard_curve.get(br, 99.0)
        return 99.0

    monkeypatch.setattr(m, "encode_opus", fake_encode)
    monkeypatch.setattr(m, "measure_sisdr", fake_measure)
    monkeypatch.setattr(m.tempfile, "mkdtemp",
                        lambda prefix="": str(tmp_path))
    monkeypatch.setattr(m.shutil, "rmtree", lambda *a, **k: None)
    return calls


def test_pick_ranks_hardest_places(monkeypatch, tmp_path):
    # only rank_5 is hard (needs 96k); easy places need 24k.
    # refine runs on hard centers, pick covers the hardest.
    hard = {24: 5.0, 32: 8.0, 40: 12.0, 48: 14.0, 56: 14.9,
            64: 15.0, 72: 16.0, 80: 18.0, 96: 20.0}
    calls = _mock_ranked_search(monkeypatch, tmp_path, {5}, 99.0, hard)
    br, method, score, pct = m.pick_audio_bitrate(
        "f", _track(), 15.0, 3600.0)
    assert (br, method) == (64, "search")
    assert score == 15.0
    assert len(set(calls["refine_extracts"])) == 3


def test_pick_bisect_budget(monkeypatch, tmp_path):
    # every place hard with a rising curve: bisection must find the
    # exact first hit with few probes, not a full ladder walk.
    hard = {24: 5.0, 32: 8.0, 40: 12.0, 48: 14.0, 56: 14.9,
            64: 15.0, 72: 16.0, 80: 18.0, 96: 20.0, 112: 21.0,
            128: 22.0}
    calls = _mock_ranked_search(monkeypatch, tmp_path,
                                set(range(30)), 99.0, hard)
    br, method, _score, _pct = m.pick_audio_bitrate(
        "f", _track(), 15.0, 3600.0)
    assert (br, method) == (64, "search")
    # 30 rank probes + 3 refines x at most 8 probes each
    assert calls["encode"] <= 30 + 3 * 8


def test_pick_max_across_places(monkeypatch, tmp_path):
    # hardest place decides: pick is the max over refined places.
    hard = {24: 5.0, 32: 8.0, 40: 12.0, 48: 14.0, 56: 14.9,
            64: 15.0, 72: 16.0, 80: 18.0, 96: 20.0}
    calls = _mock_ranked_search(monkeypatch, tmp_path, {5}, 99.0, hard)
    br, method, _score, _pct = m.pick_audio_bitrate(
        "f", _track(), 18.0, 3600.0)
    assert (br, method) == (80, "search")


def test_pick_median_of_hard_places(monkeypatch, tmp_path):
    # refine_0 is hard (needs 80k at target 18), refine_1/2 are easy
    # (need 24k): median picks 24k, not the max.
    _asisdr_ok(monkeypatch)
    hard = {24: 5.0, 32: 8.0, 40: 12.0, 48: 14.0, 56: 14.9,
            64: 15.0, 72: 16.0, 80: 18.0, 96: 20.0}
    monkeypatch.setattr(m, "extract_sample", lambda *a, **k: True)
    monkeypatch.setattr(m, "active_channels", lambda ref: [0])
    monkeypatch.setattr(m, "encode_opus", lambda ref, br, out: True)

    def fake_measure(ref, enc, active):
        import re
        mm = re.search(r"enc_(\d+)", enc)
        br = int(mm.group(1)) if mm else 0
        if "rank_5.wav" in ref or "refine_0.wav" in ref:
            return hard.get(br, 99.0)
        return 99.0

    monkeypatch.setattr(m, "measure_sisdr", fake_measure)
    monkeypatch.setattr(m.tempfile, "mkdtemp",
                        lambda prefix="": str(tmp_path))
    monkeypatch.setattr(m.shutil, "rmtree", lambda *a, **k: None)
    assert m.pick_audio_bitrate("f", _track(), 18.0,
                                3600.0) == (24, "search", 99.0, 19)


def test_auto_pick_mode():
    assert m.auto_pick_mode({}) is None
    assert m.auto_pick_mode({"removegrain": None, "fftdnoiz": None}) is None
    assert m.auto_pick_mode({"removegrain": 0.03, "fftdnoiz": 0.02}) is None
    assert m.auto_pick_mode({"removegrain": 0.06,
                             "fftdnoiz": 0.02}) == "removegrain:4"
    assert m.auto_pick_mode({"removegrain": 0.15,
                             "fftdnoiz": 0.02}) == "removegrain:8"
    assert m.auto_pick_mode({"removegrain": 0.06,
                             "fftdnoiz": 0.12}) == "fftdnoiz:8"
    assert m.auto_pick_mode({"removegrain": 0.3}) == "removegrain:12"
    assert m.auto_pick_mode({"removegrain": 0.08}) == "removegrain:4"
    assert m.auto_pick_mode(None) is None
    assert m.auto_pick_mode("bad") is None
    # over-budget filters are out even when winning on removal
    assert m.auto_pick_mode({"removegrain": 0.3, "fftdnoiz": 0.1},
                            {"removegrain": 120.0,
                             "fftdnoiz": 5.0}) == "fftdnoiz:8"


def test_format_grain_result():
    assert m.format_grain_result(None, None) == "- off"
    assert m.format_grain_result(None, 0.3) == "- off"
    assert m.format_grain_result("removegrain:8", 0.12) == \
        "- removegrain, m0 2:m1 2:m2 2 (12%)"
    assert m.format_grain_result("hqdn3d-strong:12", 0.34) == \
        "- hqdn3d-strong, 5:5:8:8 (34%)"
    assert m.format_grain_result("atadenoise:8", 0.03) == \
        "- atadenoise (3%)"
    assert m.format_grain_result("atadenoise:8", None) == "- atadenoise"
    assert m.format_grain_result("bogus:8", 0.1) == "- off"


def test_spinner_bar_name_in_tail(monkeypatch, capsys):
    s = m._Spinner("fftdnoiz", total=27)
    s.tick()
    s.tick(done=3)
    s.finish()
    err = capsys.readouterr().err
    assert "fftdnoiz" in err
    assert "eta" in err
    assert "[" not in err and "]" not in err


def test_grain_level_for():
    assert m.grain_level_for(0.0) == 4
    assert m.grain_level_for(0.09) == 4
    assert m.grain_level_for(0.10) == 8
    assert m.grain_level_for(0.25) == 8
    assert m.grain_level_for(0.26) == 12
    assert m.grain_level_for(None) == 8
    assert m.grain_level_for(float("nan")) == 8


def test_parse_auto():
    assert m.parse_denoise("auto") == "auto"
    assert m.parse_denoise("AUTO") == "auto"


def test_measure_grain_mocked(monkeypatch, tmp_path):
    film = tmp_path / "g.mkv"
    film.write_bytes(b"0" * 1024)

    def fake_run(cmd):
        if "-f" in cmd:
            return _res(0, "", "")
        out = cmd[-1]
        base = os.path.basename(out)
        if base.startswith("raw_"):
            n = 1000
        elif "removegrain" in base:
            n = 700
        else:
            n = 850
        with open(out, "wb") as f:
            f.write(b"x" * n)
        return _res(0, "", "")

    monkeypatch.setattr(m, "run_quiet", fake_run)
    scores, times = m.measure_grain(str(film), 600.0)
    assert abs(scores["removegrain"] - 0.3) < 1e-9
    assert abs(scores["fftdnoiz"] - 0.15) < 1e-9
    assert m.auto_pick_mode(scores, times) == "removegrain:12"
    monkeypatch.setattr(m, "run_quiet", lambda cmd: _res(1, "", "e"))
    scores, _times = m.measure_grain(str(film), 600.0)
    assert set(scores) == set(m.FILTER_CANDIDATES)
    assert all(v is None for v in scores.values())
    assert m.measure_grain("/no/file.mkv", 600.0) == (
        {a: None for a in m.FILTER_CANDIDATES},
        {a: None for a in m.FILTER_CANDIDATES})
    assert m.measure_grain(str(film), 0) == (
        {a: None for a in m.FILTER_CANDIDATES},
        {a: None for a in m.FILTER_CANDIDATES})


def test_main_noise_auto_resolves(monkeypatch, tmp_path, capsys):
    film = tmp_path / "auto.mkv"
    film.write_bytes(b"0" * 1024)
    monkeypatch.setattr(sys, "argv",
                        ["mkv_encode.py", "--noise", str(film)])
    monkeypatch.setattr(m, "probe", lambda _p: (100.0, []))
    monkeypatch.setattr(m, "measure_grain",
                        lambda _p, _d, **k: ({"removegrain": 0.3,
                                              "fftdnoiz": 0.1}, {}))
    monkeypatch.setattr(m, "run_crf_search",
                        lambda *a, **k: (32.0, [], False))
    monkeypatch.setattr(m, "CACHE_DIR", str(tmp_path))

    class _Proc:
        pid = 4242

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(m, "_spawn", lambda *a, **k: _Proc())
    saved = (set(m._CLEANUP_FILES), set(m._CLEANUP_DIRS),
             list(m._CHILD_PROCS))
    m._CLEANUP_FILES.clear()
    m._CLEANUP_DIRS.clear()
    m._CHILD_PROCS.clear()
    try:
        rc = m.main()
    finally:
        m._CLEANUP_FILES.clear()
        m._CLEANUP_FILES.update(saved[0])
        m._CLEANUP_DIRS.clear()
        m._CLEANUP_DIRS.update(saved[1])
        m._CHILD_PROCS.clear()
        m._CHILD_PROCS.extend(saved[2])
    err = capsys.readouterr().err
    assert rc == 1
    assert "- removegrain" in err
    assert "grain auto" not in err
    import glob as _glob
    assert _glob.glob(str(tmp_path / "auto.mkv.*.noiseremovegrain12.json"))


def test_track_steps_budget():
    assert m._track_steps({"channels": 2}) == 30 + 3 * 8
    assert m._track_steps({"channels": 6}) == 30 + 3 * 8
    assert (m.RANK_POSITIONS, m.RANK_SECS, m.REFINE_TOP,
            m.REFINE_SECS, m.BISECT_PROBES) == (30, 5.0, 3, 10.0, 6)


def test_pick_sample_failure_copy(monkeypatch, tmp_path):
    _asisdr_ok(monkeypatch)
    monkeypatch.setattr(m.tempfile, "mkdtemp",
                        lambda prefix="": str(tmp_path))
    monkeypatch.setattr(m.shutil, "rmtree", lambda *a, **k: None)
    monkeypatch.setattr(m, "extract_sample",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("no ffmpeg")))
    assert m.pick_audio_bitrate(
        "f", _track(), 18.0, 100.0) == (None, "copy", None, 100)


# --- audio progress ---

def test_size_percent_formula():
    # single formula everywhere: 100 * new / old
    assert m.size_percent(15, 100) == 15
    assert m.size_percent(72_000, 485_000) == 15
    assert m.size_percent(100, 100) == 100
    assert m.size_percent(None, 100) is None
    assert m.size_percent(50, 0) is None
    assert m.size_percent(50, None) is None


def test_audio_progress_updates_one_line(capsys):
    p = m._AudioProgress(4)
    p.update(aindex=0, bitrate_k=48, score=12.3)
    p.update(aindex=0, bitrate_k=64, score=16.0)
    p.finish()
    err = capsys.readouterr().err
    # ab-av1 style tail: metric plus eta, no step counts
    assert "audio" in err and "48k" in err
    assert "eta" in err
    assert "1/4" not in err and "2/4" not in err
    assert "[" not in err and "]" not in err
    assert "\r" in err


def test_audio_progress_two_tracks(capsys):
    # two tracks -> two bars, one after another
    for ai in (0, 1):
        bar = m._AudioProgress(2, aindex=ai)
        bar.update(bitrate_k=32)
        bar.update(bitrate_k=48)
        bar.finish(f"- vbr 48 ({ai})")
    err = capsys.readouterr().err
    assert err.count("audio a0") >= 1
    assert err.count("audio a1") >= 1


def _patch_color_on(monkeypatch):
    monkeypatch.setattr(m, "_use_color", lambda: True)
    monkeypatch.setattr(m, "_stderr_use_color", lambda: True)


def _patch_color_off(monkeypatch):
    monkeypatch.setattr(m, "_use_color", lambda: False)
    monkeypatch.setattr(m, "_stderr_use_color", lambda: False)


def test_audio_progress_color_matches_abav1(monkeypatch, capsys):
    # on a terminal the bar uses the ab-av1 template:
    # spinner + clock + wide bar with color, message in brackets
    _patch_color_on(monkeypatch)
    bar = m._AudioProgress(13, aindex=1)
    bar.update(bitrate_k=32)
    bar.finish()
    err = capsys.readouterr().err
    assert "\x1b[" in err  # ANSI colors like ab-av1
    assert "audio a1" in err
    assert "00:00:" in err  # clock like ab-av1 elapsed_precise
    assert "(" in err and ")" in err


def test_audio_progress_plain_no_color(monkeypatch, capsys):
    _patch_color_off(monkeypatch)
    bar = m._AudioProgress(4, aindex=0)
    bar.update(bitrate_k=48)
    bar.finish()
    err = capsys.readouterr().err
    assert "\x1b[" not in err
    assert "audio a0" in err and "eta" in err
    assert "1/4" not in err


def test_progress_start_shows_instantly(capsys):
    # bar must appear at once, before any slow work: 0/total, no steps used
    bar = m._AudioProgress(13, aindex=1)
    bar.start()
    try:
        assert bar.done == 0
        err = capsys.readouterr().err
        assert "audio a1" in err and "eta ?" in err
    finally:
        bar.finish()


def test_progress_stage_names_step_without_advancing(capsys):
    bar = m._AudioProgress(13, aindex=0)
    bar.start()
    try:
        bar.stage("estimate")
        assert bar.done == 0
        bar.update(bitrate_k=72)
        assert bar.done == 1
        err = capsys.readouterr().err
        assert "estimate" in err
        assert "72k" in err
    finally:
        bar.finish()


def test_progress_heartbeat_stops_on_finish(monkeypatch, capsys):
    # on a real screen a background pulse moves the spinner during
    # slow steps; finish must stop it without hanging
    import time as _time
    monkeypatch.setattr(m, "_use_color", lambda: True)
    monkeypatch.setattr(m, "_stderr_use_color", lambda: True)
    bar = m._AudioProgress(13, aindex=0)
    bar.start()
    try:
        assert bar._beat is not None
        assert bar._beat.is_alive()
        _time.sleep(0.5)
        err = capsys.readouterr().err
        assert "audio a0" in err
    finally:
        bar.finish()
    assert bar._beat is None


def test_spinner_plain_lifecycle(capsys):
    s = m._Spinner("grain probe")
    s.start()
    s.note("grain probe 2/3")
    s.tick()
    s.finish()
    err = capsys.readouterr().err
    assert "grain probe" in err and "2/3" in err
    assert "\r" in err
    assert "\x1b[" not in err
    s.finish()  # second finish is safe


def test_spinner_color_uses_abav1_style(monkeypatch, capsys):
    monkeypatch.setattr(m, "_use_color", lambda: True)
    s = m._Spinner("copying audio")
    try:
        s.start()
        err = capsys.readouterr().err
        assert "\x1b[" in err
        assert "copying audio" in err
        assert "00:00:" in err
    finally:
        monkeypatch.setattr(m, "_use_color", lambda: False)
        s.finish()
        capsys.readouterr()


def test_heartbeat_stops_for_spinner(monkeypatch):
    import time as _time
    monkeypatch.setattr(m, "_use_color", lambda: True)
    s = m._Spinner("x")
    s.start()
    try:
        assert s._beat is not None and s._beat.is_alive()
        _time.sleep(0.4)
    finally:
        monkeypatch.setattr(m, "_use_color", lambda: False)
        s.finish()
    assert s._beat is None


def test_need_nl_flag_transitions(capsys):
    m._NEED_NL = False
    bar = m._AudioProgress(4, aindex=0)
    bar.update(bitrate_k=48)
    assert m._NEED_NL is True
    bar.finish("- vbr 48")
    assert m._NEED_NL is False
    capsys.readouterr()
    s = m._Spinner("x")
    s.start()
    assert m._NEED_NL is True
    s.finish()
    assert m._NEED_NL is False
    capsys.readouterr()
    m._NEED_NL = False


def test_restore_terminal_tty_and_plain(monkeypatch, capsys):
    writes = []
    m._NEED_NL = True

    class _FakeErr:
        def isatty(self):
            return True

        def write(self, text):
            writes.append(text)

        def flush(self):
            pass

    monkeypatch.setattr(m.sys, "stderr", _FakeErr())
    m._restore_terminal()
    blob = "".join(writes)
    assert "\x1b[?25h" in blob  # cursor back
    assert blob.endswith("\n")  # half-drawn line finished
    assert m._NEED_NL is False
    capsys.readouterr()

    writes.clear()
    m._NEED_NL = True

    class _Pipe:
        def isatty(self):
            return False

        def write(self, text):
            writes.append(text)

        def flush(self):
            pass

    monkeypatch.setattr(m.sys, "stderr", _Pipe())
    m._restore_terminal()
    assert writes == []
    assert m._NEED_NL is False
    capsys.readouterr()
    m._NEED_NL = False


def test_measure_grain_reports_progress(monkeypatch, tmp_path):
    film = tmp_path / "g.mkv"
    film.write_bytes(b"0" * 1024)
    seen = []

    class _Rec:
        def start(self, *a):
            seen.append("start")

        def note(self, text):
            seen.append(text)

        def finish(self, *a):
            seen.append("finish")

    def fake_run(cmd):
        if "-f" in cmd:
            return _res(0, "", "")
        with open(cmd[-1], "wb") as f:
            f.write(b"x" * 1000)
        return _res(0, "", "")

    monkeypatch.setattr(m, "run_quiet", fake_run)
    m.measure_grain(str(film), 600.0, progress=_Rec())
    assert seen and seen[0] == "start"
    assert any("1/3" in s for s in seen[1:])
    assert any("hqdn3d" in s for s in seen[1:])
    assert seen[-1] == "finish"


def test_palette_named_not_inline():
    # colors live in named constants (fb2opt order), call sites use _paint
    assert m._C_SPIN == "\033[36;1m"
    assert m._C_FILL == "\033[36m"
    assert m._C_REST == "\033[34m"
    assert m._C_RESET == "\033[0m"
    assert m._paint("x", m._C_FILL) in ("x", m._C_FILL + "x" + m._C_RESET)


def test_paint_resets_and_gates(monkeypatch):
    _patch_color_on(monkeypatch)
    assert m._paint("hi", m._C_BOLD) == m._C_BOLD + "hi" + m._C_RESET
    _patch_color_off(monkeypatch)
    assert m._paint("hi", m._C_BOLD) == "hi"
    assert m._paint("", m._C_BOLD) == ""


def test_term_width_reads_stderr(monkeypatch):
    # width comes from our own screen (stderr), re-read each frame
    bar = m._AudioProgress(4)
    monkeypatch.setattr(m.os, "get_terminal_size",
                        lambda fd: __import__("os").terminal_size((123, 24)))
    assert bar._term_width() == 123
    def _boom(_fd):
        raise OSError("no tty")
    monkeypatch.setattr(m.os, "get_terminal_size", _boom)
    monkeypatch.setattr(m.shutil, "get_terminal_size",
                        lambda: __import__("os").terminal_size((77, 24)))
    assert bar._term_width() == 77


def test_crf_search_stderr_inherited(monkeypatch):
    # ab-av1 output is never intercepted: stderr stays on the real
    # screen (None), stdout alone is piped as NDJSON
    seen = {}

    def fake_popen(cmd, **k):
        seen["kwargs"] = k
        return _FakePopen(
            ['{"type":"crf-search-done","crf":40.0,"vmaf":94.0}\n'])

    monkeypatch.setattr(m, "ensure_abav1_temp", lambda: "/tmp/fake_abav1")
    monkeypatch.setattr(m.subprocess, "Popen", fake_popen)
    crf, _, _ = m.run_crf_search("f.mkv", 94)
    assert crf == 40.0
    assert seen["kwargs"].get("stderr") is None


# --- cleanup ---

def test_cleanup_stale_tmp_encode(tmp_path):
    film = tmp_path / "film.mkv"
    film.write_bytes(b"x")
    stale = tmp_path / "film_tmp_encode.mkv"
    stale.write_bytes(b"junk")
    assert m.cleanup_stale_temps(str(film)) is True
    assert not stale.exists()


def _backdate(path):
    import time as _time
    old = _time.time() - m._STALE_SECS - 60
    os.utime(path, (old, old))


def test_is_stale():
    assert m._is_stale("/no/such/path") is True
    assert m._STALE_SECS == 3600


def test_cleanup_stale_abav1_dirs(tmp_path, monkeypatch):
    film = tmp_path / "film.mkv"
    film.write_bytes(b"x")
    leftover = tmp_path / ".ab-av1-oldRunXYZ"
    leftover.mkdir()
    (leftover / "sample.mkv").write_bytes(b"1")
    _backdate(leftover)
    cwd_leftover = tmp_path / ".ab-av1-cwdJunk"
    # pretend cwd is tmp_path so we also sweep "cwd" leftovers
    monkeypatch.chdir(tmp_path)
    cwd_leftover.mkdir()
    _backdate(cwd_leftover)
    live = tmp_path / ".ab-av1-liveRun"
    live.mkdir()
    assert m.cleanup_stale_temps(str(film)) is True
    assert not leftover.exists()
    assert not cwd_leftover.exists()
    assert live.exists()


def test_free_memory_mb_shape():
    ram, swap = m.free_memory_mb()
    assert ram is None or (isinstance(ram, int) and ram >= 0)
    assert swap is None or (isinstance(swap, int) and swap >= 0)


def test_cleanup_stale_abav1_tmp_files(tmp_path, monkeypatch):
    film = tmp_path / "film.mkv"
    film.write_bytes(b"x")
    stale = tmp_path / ".tmp.ab-av1-encoding.film_tmp_encode.mkv"
    stale.write_bytes(b"junk")
    _backdate(stale)
    fresh = tmp_path / ".tmp.ab-av1-encoding.live.mkv"
    fresh.write_bytes(b"live")
    keep = tmp_path / "other.mkv"
    keep.write_bytes(b"keep")
    monkeypatch.chdir(tmp_path)
    assert m.cleanup_stale_temps(str(film)) is True
    assert not stale.exists()
    assert fresh.exists()
    assert keep.exists()


def test_main_copy_path_shows_spinner(monkeypatch, tmp_path, capsys):
    import subprocess as _sp
    film = tmp_path / "cp.mkv"
    film.write_bytes(b"0" * 1024)
    monkeypatch.setattr(sys, "argv",
                        ["mkv_encode.py", "--vmaf", "94", "--sdr", "36",
                         str(film)])
    monkeypatch.setattr(m, "probe",
                        lambda _p: (100.0, [{"aindex": 0, "channels": 2,
                                             "codec": "aac", "bit_rate": 128000,
                                             "lang": "eng"}]))
    monkeypatch.setattr(m, "pick_audio_bitrate",
                        lambda *a, **k: (48, "search", 18.0, 38))
    monkeypatch.setattr(m, "run_crf_search",
                        lambda *a, **k: (None, [(90.0, 90.0)], True))
    monkeypatch.setattr(m, "CACHE_DIR", str(tmp_path))

    calls = {"n": 0}

    class _Proc:
        pid = 4242

        def wait(self, timeout=None):
            calls["n"] += 1
            if timeout is not None and calls["n"] == 1:
                raise _sp.TimeoutExpired("ffmpeg", timeout)
            return 0

    monkeypatch.setattr(m, "_spawn", lambda *a, **k: _Proc())
    saved = (set(m._CLEANUP_FILES), set(m._CLEANUP_DIRS),
             list(m._CHILD_PROCS))
    m._CLEANUP_FILES.clear()
    m._CLEANUP_DIRS.clear()
    m._CHILD_PROCS.clear()
    try:
        rc = m.main()
    finally:
        m._CLEANUP_FILES.clear()
        m._CLEANUP_FILES.update(saved[0])
        m._CLEANUP_DIRS.clear()
        m._CLEANUP_DIRS.update(saved[1])
        m._CHILD_PROCS.clear()
        m._CHILD_PROCS.extend(saved[2])
    err = capsys.readouterr().err
    assert calls["n"] >= 2  # first poll timed out, spinner pulsed
    assert "copying audio" in err


def test_main_encode_failure_reports(monkeypatch, tmp_path, capsys):
    film = tmp_path / "enc.mkv"
    film.write_bytes(b"0" * 1024)
    monkeypatch.setattr(sys, "argv",
                        ["mkv_encode.py", "--vmaf", "94", "--sdr", "36",
                         str(film)])
    monkeypatch.setattr(m, "probe", lambda _p: (100.0, []))
    monkeypatch.setattr(m, "run_crf_search",
                        lambda *a, **k: (32.0, [], False))
    monkeypatch.setattr(m, "CACHE_DIR", str(tmp_path))

    class _Proc:
        pid = 4242

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(m, "_spawn", lambda *a, **k: _Proc())
    saved = (set(m._CLEANUP_FILES), set(m._CLEANUP_DIRS),
             list(m._CHILD_PROCS))
    m._CLEANUP_FILES.clear()
    m._CLEANUP_DIRS.clear()
    m._CHILD_PROCS.clear()
    try:
        rc = m.main()
    finally:
        m._CLEANUP_FILES.clear()
        m._CLEANUP_FILES.update(saved[0])
        m._CLEANUP_DIRS.clear()
        m._CLEANUP_DIRS.update(saved[1])
        m._CHILD_PROCS.clear()
        m._CHILD_PROCS.extend(saved[2])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Encode failed" in err


def test_cleanup_generated_removes_registered(tmp_path):
    f = tmp_path / "out_tmp_encode.mkv"
    f.write_bytes(b"x")
    d = tmp_path / "mkv_audio_test"
    d.mkdir()
    (d / "ref.wav").write_bytes(b"1")
    m._CLEANUP_FILES.clear()
    m._CLEANUP_DIRS.clear()
    m._register_file(str(f))
    m._register_dir(str(d))
    m.cleanup_generated()
    assert not f.exists()
    assert not d.exists()


# --- run_crf_search ---

class _FakePopen:
    pid = None

    def __init__(self, lines, returncode=0, kill_log=None):
        self._lines = lines
        self.returncode = returncode
        self._kill_log = kill_log
        self.stdout = self

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        if self._kill_log is not None:
            self._kill_log.append(True)


def test_crf_search_parses_json_done(monkeypatch):
    lines = [
        '{"type":"sample-encode-done","crf":37.5,"vmaf":97.06,"predicted_encode_percent":50.0}\n',
        '{"type":"sample-encode-done","crf":63.75,"vmaf":94.22,"predicted_encode_percent":30.0}\n',
        '{"type":"crf-search-done","crf":63.75,"vmaf":94.22,"predicted_encode_percent":30.0}\n',
    ]
    seen = {}

    def fake_popen(cmd, **k):
        seen["cmd"] = cmd
        seen["kwargs"] = k
        return _FakePopen(lines)

    monkeypatch.setattr(m, "ensure_abav1_temp", lambda: "/tmp/fake_abav1")
    monkeypatch.setattr(m.subprocess, "Popen", fake_popen)
    crf, samples, no_good = m.run_crf_search("f.mkv", 94)
    assert crf == 63.75
    assert no_good is False
    assert len(samples) == 2
    # stderr must stay inherited (None) so ab-av1 keeps its TTY bar
    assert seen["kwargs"].get("stderr") is None
    assert "--stdout-format" in seen["cmd"]
    assert "json" in seen["cmd"]
    assert "--temp-dir" in seen["cmd"]
    assert "/tmp/fake_abav1" in seen["cmd"]
    assert seen["kwargs"].get("start_new_session") is True


def test_crf_search_passes_svt_args(monkeypatch):
    seen = {}

    def fake_popen(cmd, **k):
        seen["cmd"] = cmd
        return _FakePopen(
            ['{"type":"crf-search-done","crf":40.0,"vmaf":94.0}\n'])

    monkeypatch.setattr(m, "ensure_abav1_temp", lambda: "/tmp/fake_abav1")
    monkeypatch.setattr(m.subprocess, "Popen", fake_popen)
    crf, _samples, _no_good = m.run_crf_search(
        "f.mkv", 94,
        svt_args=["film-grain=8", "film-grain-denoise=1"])
    assert crf == 40.0
    assert seen["cmd"].count("--svt") == 2
    assert "film-grain=8" in seen["cmd"]
    assert "film-grain-denoise=1" in seen["cmd"]


def test_crf_search_passes_vfilter(monkeypatch):
    seen = {}

    def fake_popen(cmd, **k):
        seen["cmd"] = cmd
        return _FakePopen(
            ['{"type":"crf-search-done","crf":40.0,"vmaf":94.0}\n'])

    monkeypatch.setattr(m, "ensure_abav1_temp", lambda: "/tmp/fake_abav1")
    monkeypatch.setattr(m.subprocess, "Popen", fake_popen)
    crf, _, _ = m.run_crf_search("f.mkv", 94, svt_args=["film-grain=8",
                                 "film-grain-denoise=0"],
                                 vfilter="hqdn3d=2:2:4:4")
    assert crf == 40.0
    assert "--vfilter" in seen["cmd"]
    assert "hqdn3d=2:2:4:4" in seen["cmd"]
    crf, _, _ = m.run_crf_search("f.mkv", 94)
    assert "--vfilter" not in seen["cmd"]


def test_crf_search_noise_alias_and_level(monkeypatch):
    seen = {}

    def fake_popen(cmd, **k):
        seen["cmd"] = cmd
        return _FakePopen(
            ['{"type":"crf-search-done","crf":41.0,"vmaf":94.0}\n'])

    monkeypatch.setattr(m, "ensure_abav1_temp", lambda: "/tmp/fake_abav1")
    monkeypatch.setattr(m.subprocess, "Popen", fake_popen)
    # deprecated bool alias maps to the hqdn3d default
    crf, _, _ = m.run_crf_search("f.mkv", 94, noise=True)
    assert crf == 41.0
    assert "film-grain=8" in seen["cmd"]
    assert "film-grain-denoise=0" in seen["cmd"]
    # explicit level flows through unchanged
    crf, _, _ = m.run_crf_search(
        "f.mkv", 94, svt_args=m.svt_args_for_denoise("removegrain:12"))
    assert crf == 41.0
    assert "film-grain=12" in seen["cmd"]
    # no denoise -> no --svt flags at all
    crf, _, _ = m.run_crf_search("f.mkv", 94)
    assert crf == 41.0
    assert "--svt" not in seen["cmd"]


def test_crf_search_failure_rc(monkeypatch):
    monkeypatch.setattr(m, "ensure_abav1_temp", lambda: "/tmp/fake_abav1")
    monkeypatch.setattr(m.subprocess, "Popen",
                        lambda *a, **k: _FakePopen([], returncode=1))
    crf, _samples, _no_good = m.run_crf_search("f.mkv", 94)
    assert crf is None


def test_crf_search_no_binary(monkeypatch):
    monkeypatch.setattr(m, "ensure_abav1_temp", lambda: "/tmp/fake_abav1")

    def boom(*a, **k):
        raise FileNotFoundError("no ab-av1")
    monkeypatch.setattr(m.subprocess, "Popen", boom)
    crf, _samples, _no_good = m.run_crf_search("f.mkv", 94)
    assert crf is None


def test_crf_search_kills_child_on_abort(monkeypatch):
    log = []

    class _BoomStdout(_FakePopen):
        pid = 4242

        def __iter__(self):
            raise RuntimeError("interrupted")

    monkeypatch.setattr(m, "ensure_abav1_temp", lambda: "/tmp/fake_abav1")
    monkeypatch.setattr(m.subprocess, "Popen",
                        lambda *a, **k: _BoomStdout([]))
    monkeypatch.setattr(m.os, "killpg",
                        lambda pid, sig: log.append((pid, sig)))
    try:
        m.run_crf_search("f.mkv", 94)
        assert False, "must re-raise"
    except RuntimeError:
        pass
    assert log and log[0][0] == 4242


def test_crf_search_guards():
    assert m.run_crf_search(None, 94) == (None, [], False)
    assert m.run_crf_search("f.mkv", None) == (None, [], False)


# --- main arg validation ---

def test_main_no_input(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mkv_encode.py"])
    assert m.main() == 1
    assert "Usage" in capsys.readouterr().err


def test_main_missing_file(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mkv_encode.py", "/no/file.mkv"])
    assert m.main() == 1
    assert "not found" in capsys.readouterr().err


def _run_main_no_audio(monkeypatch, tmp_path, crf_result, argv_extra=None):
    """Run main() on a file with no audio tracks; return (rc, err, cache)."""
    import json as _json
    film = tmp_path / "miss.mkv"
    film.write_bytes(b"0" * 1024)
    argv = ["mkv_encode.py", "--vmaf", "94", "--sdr", "36", str(film)]
    if argv_extra:
        argv.extend(argv_extra)
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(m, "probe", lambda _p: (100.0, []))
    monkeypatch.setattr(m, "run_crf_search", lambda *a, **k: crf_result)
    monkeypatch.setattr(m, "CACHE_DIR", str(tmp_path))
    import io
    rc = m.main()
    return rc, film


def test_main_video_miss_remembers_copy(monkeypatch, tmp_path, capsys):
    # genuine miss: target needs over 80% of source; copy is remembered
    rc, film = _run_main_no_audio(
        monkeypatch, tmp_path, (None, [(93.0, 90.0)], True))
    err = capsys.readouterr().err
    assert rc == 1
    assert "80%" in err and "93.00" in err and "lower --vmaf" in err
    import json as _json
    caches = list(tmp_path.glob("miss.mkv.*.json"))
    assert len(caches) == 1
    assert _json.loads(caches[0].read_text())["video"] == "copy"


def test_main_video_crash_missing_input(monkeypatch, tmp_path, capsys):
    # input vanishes mid-run (sleeping drive): message says so plainly
    import os as _os
    film = tmp_path / "gone.mkv"
    film.write_bytes(b"0" * 1024)
    monkeypatch.setattr(sys, "argv",
                        ["mkv_encode.py", "--vmaf", "94", "--sdr", "36",
                         str(film)])
    monkeypatch.setattr(m, "probe", lambda _p: (100.0, []))

    def _boom(*a, **k):
        _os.remove(str(film))
        return None, [], False

    monkeypatch.setattr(m, "run_crf_search", _boom)
    monkeypatch.setattr(m, "CACHE_DIR", str(tmp_path))
    assert m.main() == 1
    assert "gone mid-run" in capsys.readouterr().err


def test_main_video_crash_retries_next_run(monkeypatch, tmp_path, capsys):
    # transient failure: this run keeps video, next run searches again
    rc, film = _run_main_no_audio(monkeypatch, tmp_path, (None, [], False))
    err = capsys.readouterr().err
    assert rc == 1
    assert "search again" in err
    import json as _json
    caches = list(tmp_path.glob("miss.mkv.*.json"))
    assert len(caches) == 1
    assert "video" not in _json.loads(caches[0].read_text())


def test_main_noise_flag_needs_no_value(monkeypatch, tmp_path, capsys):
    import pytest as _pytest
    fake = tmp_path / "f.mkv"
    fake.write_bytes(b"0")
    monkeypatch.setattr(sys, "argv",
                        ["mkv_encode.py", "--noise", "hqdn3d", str(fake)])
    with _pytest.raises(SystemExit) as _exc:
        m.main()
    assert _exc.value.code == 2
    assert "unrecognized" in capsys.readouterr().err


def test_main_crop_chain(monkeypatch, tmp_path, capsys):
    film = tmp_path / "crop.mkv"
    film.write_bytes(b"0" * 1024)
    monkeypatch.setattr(sys, "argv",
                        ["mkv_encode.py", "--crop", str(film)])
    monkeypatch.setattr(m, "probe", lambda _p: (100.0, []))
    monkeypatch.setattr(m, "detect_crop",
                        lambda _p, _d, **k: "1920:800:0:140")
    seen = {}

    def fake_search(*a, **k):
        seen.update(k)
        return 32.0, [], False

    monkeypatch.setattr(m, "run_crf_search", fake_search)
    monkeypatch.setattr(m, "CACHE_DIR", str(tmp_path))

    class _Proc:
        pid = 4242

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(m, "_spawn", lambda *a, **k: _Proc())
    saved = (set(m._CLEANUP_FILES), set(m._CLEANUP_DIRS),
             list(m._CHILD_PROCS))
    m._CLEANUP_FILES.clear()
    m._CLEANUP_DIRS.clear()
    m._CHILD_PROCS.clear()
    try:
        rc = m.main()
    finally:
        m._CLEANUP_FILES.clear()
        m._CLEANUP_FILES.update(saved[0])
        m._CLEANUP_DIRS.clear()
        m._CLEANUP_DIRS.update(saved[1])
        m._CHILD_PROCS.clear()
        m._CHILD_PROCS.extend(saved[2])
    err = capsys.readouterr().err
    assert rc == 1
    assert "- crop 1920:800:0:140" in err
    assert seen.get("vfilter") == "crop=1920:800:0:140"
    import glob as _glob
    assert _glob.glob(str(tmp_path / "crop.mkv.*.crop1920x800.json"))


def test_main_bad_sdr(monkeypatch, tmp_path, capsys):
    fake = tmp_path / "f.mkv"
    fake.write_bytes(b"0")
    monkeypatch.setattr(sys, "argv",
                        ["mkv_encode.py", "--sdr", "120", str(fake)])
    assert m.main() == 1
    assert "--sdr" in capsys.readouterr().err
