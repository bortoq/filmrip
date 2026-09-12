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
    _write(p, '{"crf": 55.5, "audio": [[80, "search"], [null, "copy"]]}')
    assert m.load_cache(p) == (55.5, [(80, "search"), (None, "copy")])


def test_load_cache_audio_only(tmp_path):
    p = str(tmp_path / "c.json")
    _write(p, '{"crf": null, "audio": [[48, "search"]]}')
    assert m.load_cache(p) == (None, [(48, "search")])


def test_load_cache_missing():
    assert m.load_cache("/no/cache.json") == (None, None)


def test_load_cache_garbage(tmp_path):
    p = str(tmp_path / "c.json")
    _write(p, "{oops")
    assert m.load_cache(p) == (None, None)


def test_load_cache_bad_shapes(tmp_path):
    for bad_audio in ['"ab"', '{"x": 1}', '5', '[["a"]]', '[[80]]',
                      '[[80, "nope"]]', '[["80", "search"]]', "[[80]]"]:
        p = str(tmp_path / "c.json")
        _write(p, '{"crf": 55, "audio": %s}' % bad_audio)
        assert m.load_cache(p) == (None, None), bad_audio


def test_load_cache_bad_crf(tmp_path):
    p = str(tmp_path / "c.json")
    _write(p, '{"crf": "abc", "audio": []}')
    assert m.load_cache(p) == (None, None)


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
                                18.0, 100.0) == (None, None, "copy")


def test_pick_no_meter_fallback(monkeypatch):
    _asisdr_ok(monkeypatch, ok=False)
    assert m.pick_audio_bitrate("f", _track(), 18.0,
                                100.0) == (48, None, "fallback")


def test_pick_unknown_channels_fallback(monkeypatch):
    _asisdr_ok(monkeypatch)
    br, score, method = m.pick_audio_bitrate(
        "f", _track(channels=0), 18.0, 100.0)
    assert (br, method) == (64, "fallback")


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
    # stereo ladder: 24/32/40 fail, 48 hits target 15 -> 48k
    _mock_search(monkeypatch,
                 {24: 10.0, 32: 14.0, 40: 14.5, 48: 18.3}, tmp_path)
    assert m.pick_audio_bitrate("f", _track(), 15.0,
                                100.0) == (48, 18.3, "search")


def test_pick_search_copy_when_source_lean(monkeypatch, tmp_path):
    # source 48k aac, search would take 48k -> copy instead
    _mock_search(monkeypatch,
                 {24: 10.0, 32: 14.0, 40: 14.5, 48: 18.3}, tmp_path)
    assert m.pick_audio_bitrate("f", _track(bit_rate=48000), 15.0,
                                100.0) == (None, None, "copy")


def test_pick_max_ceiling(monkeypatch, tmp_path):
    _mock_search(monkeypatch, {}, tmp_path)
    monkeypatch.setattr(m, "measure_sisdr",
                        lambda ref, enc, active: 0.0)
    br, score, method = m.pick_audio_bitrate(
        "f", _track(codec="flac", bit_rate=900000), 50.0, 100.0)
    assert (br, method) == (192, "max")


def test_pick_max_prefers_copy(monkeypatch, tmp_path):
    _mock_search(monkeypatch, {}, tmp_path)
    monkeypatch.setattr(m, "measure_sisdr",
                        lambda ref, enc, active: 0.0)
    # 64k aac capped at 64k ceiling: same bytes -> copy, no re-encode loss
    assert m.pick_audio_bitrate("f", _track(bit_rate=64000), 50.0,
                                100.0) == (None, None, "copy")


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
    # unknown-bitrate aac guessed at 64k, target unreachable:
    # ceiling equals the guess -> copy instead of same-size re-encode
    assert m.pick_audio_bitrate("f", _track(bit_rate=None), 50.0,
                                100.0) == (None, None, "copy")


def test_pick_sample_failure_fallback(monkeypatch, tmp_path):
    _asisdr_ok(monkeypatch)
    monkeypatch.setattr(m.tempfile, "mkdtemp",
                        lambda prefix="": str(tmp_path))
    monkeypatch.setattr(m.shutil, "rmtree", lambda *a, **k: None)
    monkeypatch.setattr(m, "extract_sample",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("no ffmpeg")))
    br, score, method = m.pick_audio_bitrate("f", _track(), 18.0, 100.0)
    assert (br, method) == (48, "fallback")


# --- audio progress ---

def test_audio_progress_updates_one_line(capsys):
    p = m._AudioProgress(4)
    p.update(aindex=0, bitrate_k=48, score=12.3)
    p.update(aindex=0, bitrate_k=64, score=16.0)
    p.finish()
    err = capsys.readouterr().err
    assert "audio" in err and "48k" in err and "(25%)" in err
    assert "\r" in err


# --- cleanup ---

def test_cleanup_stale_tmp_encode(tmp_path):
    film = tmp_path / "film.mkv"
    film.write_bytes(b"x")
    stale = tmp_path / "film_tmp_encode.mkv"
    stale.write_bytes(b"junk")
    assert m.cleanup_stale_temps(str(film)) is True
    assert not stale.exists()


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
        '{"type":"sample-encode-done","crf":37.5,"vmaf":97.06}\n',
        '{"type":"sample-encode-done","crf":63.75,"vmaf":94.22}\n',
        '{"type":"crf-search-done","crf":63.75,"vmaf":94.22}\n',
    ]
    seen = {}

    def fake_popen(cmd, **k):
        seen["cmd"] = cmd
        seen["kwargs"] = k
        return _FakePopen(lines)

    monkeypatch.setattr(m.subprocess, "Popen", fake_popen)
    assert m.run_crf_search("f.mkv", 94) == 63.75
    # stderr must stay inherited (None) so ab-av1 keeps its TTY bar
    assert seen["kwargs"].get("stderr") is None
    assert "--stdout-format" in seen["cmd"]
    assert "json" in seen["cmd"]


def test_crf_search_failure_rc(monkeypatch):
    monkeypatch.setattr(m.subprocess, "Popen",
                        lambda *a, **k: _FakePopen([], returncode=1))
    assert m.run_crf_search("f.mkv", 94) is None


def test_crf_search_no_binary(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("no ab-av1")
    monkeypatch.setattr(m.subprocess, "Popen", boom)
    assert m.run_crf_search("f.mkv", 94) is None


def test_crf_search_kills_child_on_abort(monkeypatch):
    log = []

    class _BoomStdout(_FakePopen):
        def __iter__(self):
            raise RuntimeError("interrupted")

    monkeypatch.setattr(m.subprocess, "Popen",
                        lambda *a, **k: _BoomStdout([], kill_log=log))
    try:
        m.run_crf_search("f.mkv", 94)
        assert False, "must re-raise"
    except RuntimeError:
        pass
    assert log == [True]


def test_crf_search_guards():
    assert m.run_crf_search(None, 94) is None
    assert m.run_crf_search("f.mkv", None) is None


# --- main arg validation ---

def test_main_no_input(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mkv_encode.py"])
    assert m.main() == 1
    assert "Usage" in capsys.readouterr().err


def test_main_missing_file(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mkv_encode.py", "/no/file.mkv"])
    assert m.main() == 1
    assert "not found" in capsys.readouterr().err


def test_main_bad_sdr(monkeypatch, tmp_path, capsys):
    fake = tmp_path / "f.mkv"
    fake.write_bytes(b"0")
    monkeypatch.setattr(sys, "argv",
                        ["mkv_encode.py", "--sdr", "120", str(fake)])
    assert m.main() == 1
    assert "--sdr" in capsys.readouterr().err
