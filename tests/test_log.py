"""Tests for macrolens_log: log format v2, the Recorder state machine, sealing and the instance lock.

Fake clocks throughout, no sleeps. The v2 helpers below (read_lines, parse_log, log_files,
sweep_file) are plain functions so the fake-OBS integration tests can reuse them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import macrolens_log
from macrolens_log import (
    COLUMNS,
    EVENT_KINDS,
    INPUT_EVENT_KINDS,
    LOCK_FILE_NAME,
    LOG_NAME_RE,
    STOP_REASONS,
    TSV_HEADER,
    AlreadyRunning,
    Recorder,
    acquire_instance_lock,
    clean_field,
    format_time,
    seal_unfinished_logs,
    utc_offset,
)

WALL_START = 1_767_225_600.0  # about 1 Jan 2026; local time varies by runner, which is fine


# ---------------------------------------------------------------- v2 helpers (reusable)


class FakeClock:
    """A clock that only moves when told to."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def read_lines(path):
    """The lines of a log, split on \\n only. The file must end with \\n."""
    data = Path(path).read_bytes().decode("utf-8")
    assert data.endswith("\n"), f"{path} does not end with a newline"
    return data.split("\n")[:-1]


def parse_log(path):
    """Rows of a v2 log as dicts of the six columns (strings; JSON video_time formatted %.3f, meta dropped)."""
    path = str(path)
    lines = read_lines(path)
    rows = []
    if path.endswith(".tsv"):
        assert lines and lines[0] == TSV_HEADER, f"{path}: line 1 is not the header"
        for line in lines[1:]:
            fields = line.split("\t")
            assert len(fields) == 6, f"{path}: {len(fields)} fields in {line!r}"
            rows.append(dict(zip(COLUMNS, fields, strict=True)))
    else:
        for line in lines:
            obj = json.loads(line)
            obj.pop("meta", None)
            assert tuple(obj) == COLUMNS, f"{path}: keys {tuple(obj)}"
            video = obj["video_time"]
            assert isinstance(video, (int, float)) and not isinstance(video, bool)
            obj["video_time"] = "%.3f" % video
            rows.append(obj)
    return rows


def log_files(log_dir):
    """Every log file in a folder, in name order."""
    folder = Path(log_dir)
    if not folder.is_dir():
        return []
    return sorted(str(p) for p in folder.iterdir() if LOG_NAME_RE.search(p.name))


def all_events(log_dir):
    return [row["event"] for path in log_files(log_dir) for row in parse_log(path)]


def sweep_file(path):
    """Assert the v2 invariants over every line: no input outside recording, one start, ends with a stop."""
    rows = parse_log(path)
    assert rows, f"{path} has no rows"
    assert rows[0]["event"] == "RECORD_START", f"{path}: first row is {rows[0]['event']}"
    assert rows[-1]["event"] == "RECORD_STOP", f"{path}: last row is {rows[-1]['event']}"
    assert sum(1 for row in rows if row["event"] == "RECORD_START") == 1
    state = "idle"
    for row in rows:
        kind = row["event"]
        assert kind in EVENT_KINDS, kind
        assert float(row["video_time"]) >= 0
        if kind.startswith("RECORD_"):
            assert row["window"] == "" and row["position"] == ""
        if kind == "RECORD_START":
            assert state == "idle"
            state = "recording"
        elif kind == "RECORD_PAUSE":
            assert state == "recording"
            state = "paused"
        elif kind == "RECORD_RESUME":
            assert state == "paused"
            state = "recording"
        elif kind == "RECORD_STOP":
            assert state in ("recording", "paused")
            state = "idle"
            reason = row["detail"].split(" ")[0]
            assert reason.startswith("reason=") and reason[len("reason="):] in STOP_REASONS, row["detail"]
        elif kind == "NOTE":
            assert state in ("recording", "paused")
        else:
            assert kind in INPUT_EVENT_KINDS
            assert state == "recording", f"{path}: {kind} logged while {state}"
    assert state == "idle"
    return rows


def detail_pairs(detail):
    """RECORD_START metadata from a detail string, as an ordered list of (key, value)."""
    return [tuple(pair.split("=", 1)) for pair in detail.split(" ")]


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def make_recorder(tmp_path):
    """Build Recorders on fake clocks; at teardown shut each down and sweep every file it wrote."""
    made = []

    def make(log_dir=None, fmt="tsv", **kwargs):
        wall = FakeClock(WALL_START)
        mono = FakeClock(0.0)
        messages = []
        kwargs.setdefault("on_message", messages.append)
        kwargs.setdefault("window_title", lambda: "Editor")
        folder = str(log_dir if log_dir is not None else tmp_path / "logs")
        rec = Recorder(folder, fmt, clock=wall, mono_clock=mono, **kwargs)
        made.append(SimpleNamespace(rec=rec, wall=wall, mono=mono, messages=messages, dir=folder))
        return made[-1]

    yield make
    for item in made:
        item.rec.shutdown("user_quit")
    for folder in sorted({item.dir for item in made}):
        for path in log_files(folder):
            sweep_file(path)


def run_with_timeout(fn, timeout=2.0):
    """Run fn in a thread and fail if it hasn't finished in time (a deadlock)."""
    errors = []

    def target():
        try:
            fn()
        except Exception as err:  # reported below
            errors.append(err)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "did not finish: deadlock"
    assert errors == []


# ---------------------------------------------------------------- cold start, basics


def test_cold_start_creates_log_dir(make_recorder, tmp_path):
    folder = tmp_path / "new" / "logs"
    t = make_recorder(log_dir=folder)
    assert not folder.exists()
    t.rec.apply_status(False, False)
    assert not folder.exists()  # nothing is created until a recording starts
    t.rec.apply_status(True, False)
    assert folder.is_dir()
    assert os.path.dirname(t.rec.log_path) == str(folder)
    assert t.messages == [f"Recording started. Log: {t.rec.log_path}"]


def test_header_and_start_lines(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, False)
    lines = read_lines(t.rec.log_path)
    assert lines[0] == TSV_HEADER
    assert lines[1].split("\t")[2] == "RECORD_START"
    assert len(lines) == 2


def test_json_has_no_header(make_recorder):
    t = make_recorder(fmt="json")
    t.rec.apply_status(True, False)
    assert t.rec.log_path.endswith("_task_log.jsonl")
    first = json.loads(read_lines(t.rec.log_path)[0])
    assert first["event"] == "RECORD_START"
    assert first["meta"]["log_format"] == 2


def test_stop_message_and_reason(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, False)
    t.rec.apply_status(False, False, stop_reason="obs_exited")
    assert t.messages[-1] == f"Recording stopped (obs_exited). Log: {t.rec.log_path}"
    assert parse_log(t.rec.log_path)[-1]["detail"] == "reason=obs_exited"
    assert t.rec.state == "idle"


def test_last_confirmed_on_stop(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, False)
    t.rec.stop("obs_disconnected", last_confirmed=WALL_START + 1.25)
    expected = f"reason=obs_disconnected last_confirmed={format_time(WALL_START + 1.25)}"
    assert parse_log(t.rec.log_path)[-1]["detail"] == expected


def test_bad_inputs_are_refused(make_recorder, tmp_path):
    with pytest.raises(ValueError):
        Recorder(str(tmp_path), "xml")
    t = make_recorder()
    with pytest.raises(ValueError):
        t.rec.event("RECORD_START")
    with pytest.raises(ValueError):
        t.rec.stop("because")
    with pytest.raises(ValueError):
        t.rec.apply_status(True, False, stop_reason="because")
    assert t.rec.state == "idle"


def test_window_title_failure_logs_empty_window(make_recorder):
    def broken():
        raise RuntimeError("no window")

    t = make_recorder(window_title=broken)
    t.rec.apply_status(True, False)
    t.rec.event("key_down", "a")
    assert parse_log(t.rec.log_path)[-1]["window"] == ""


# ---------------------------------------------------------------- status x operation grid

SETUPS = {
    "idle": lambda r: None,
    "recording": lambda r: r.apply_status(True, False),
    "paused": lambda r: r.apply_status(True, True),
    "shut_down": lambda r: r.shutdown("user_quit"),
}

OPS = {
    "TF": lambda r: r.apply_status(True, False),
    "TT": lambda r: r.apply_status(True, True),
    "FF": lambda r: r.apply_status(False, False),
    "FT": lambda r: r.apply_status(False, True),
    "stop": lambda r: r.stop("user_quit"),
    "event": lambda r: r.event("key_down", "a"),
    "note": lambda r: r.note("hello"),
}

GRID = {
    ("idle", "TF"): ("recording", ["RECORD_START"]),
    ("idle", "TT"): ("paused", ["RECORD_START", "RECORD_PAUSE"]),
    ("idle", "FF"): ("idle", []),
    ("idle", "FT"): ("idle", []),
    ("idle", "stop"): ("idle", []),
    ("idle", "event"): ("idle", []),
    ("idle", "note"): ("idle", []),
    ("recording", "TF"): ("recording", []),
    ("recording", "TT"): ("paused", ["RECORD_PAUSE"]),
    ("recording", "FF"): ("idle", ["RECORD_STOP"]),
    ("recording", "FT"): ("idle", ["RECORD_STOP"]),
    ("recording", "stop"): ("idle", ["RECORD_STOP"]),
    ("recording", "event"): ("recording", ["key_down"]),
    ("recording", "note"): ("recording", ["NOTE"]),
    ("paused", "TF"): ("recording", ["RECORD_RESUME"]),
    ("paused", "TT"): ("paused", []),
    ("paused", "FF"): ("idle", ["RECORD_STOP"]),
    ("paused", "FT"): ("idle", ["RECORD_STOP"]),
    ("paused", "stop"): ("idle", ["RECORD_STOP"]),
    ("paused", "event"): ("paused", []),
    ("paused", "note"): ("paused", ["NOTE"]),
    ("shut_down", "TF"): ("idle", []),
    ("shut_down", "TT"): ("idle", []),
    ("shut_down", "FF"): ("idle", []),
    ("shut_down", "FT"): ("idle", []),
    ("shut_down", "stop"): ("idle", []),
    ("shut_down", "event"): ("idle", []),
    ("shut_down", "note"): ("idle", []),
}


def test_grid_is_complete():
    assert set(GRID) == {(s, o) for s in SETUPS for o in OPS}


@pytest.mark.parametrize("start,op", sorted(GRID))
def test_status_operation_grid(make_recorder, start, op):
    t = make_recorder()
    SETUPS[start](t.rec)
    before = all_events(t.dir)
    t.mono.advance(1.0)
    t.wall.advance(1.0)
    OPS[op](t.rec)
    want_state, want_new = GRID[(start, op)]
    assert t.rec.state == want_state
    assert all_events(t.dir)[len(before):] == want_new
    if start == "shut_down":
        assert t.rec.shut_down
        assert log_files(t.dir) == []


def test_start_paused_writes_start_then_pause(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, True, duration_ms=5000)
    rows = parse_log(t.rec.log_path)
    assert [r["event"] for r in rows] == ["RECORD_START", "RECORD_PAUSE"]
    assert [r["video_time"] for r in rows] == ["5.000", "5.000"]
    assert t.rec.state == "paused"


def test_stopped_while_paused_is_obs_stopped(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, True)
    t.rec.apply_status(False, True)
    assert parse_log(t.rec.log_path)[-1]["detail"] == "reason=obs_stopped"


def test_event_while_paused_writes_nothing(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, False)
    t.rec.apply_status(True, True)
    t.rec.event("key_down", "p")
    t.rec.event("mouse_down", "left", "1,2")
    assert all_events(t.dir) == ["RECORD_START", "RECORD_PAUSE"]


def test_no_new_file_after_shutdown(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, False)
    t.rec.shutdown("window_closed")
    t.wall.advance(5.0)
    t.rec.apply_status(True, False)
    assert t.rec.state == "idle"
    assert len(log_files(t.dir)) == 1
    assert parse_log(t.rec.log_path)[-1]["detail"] == "reason=window_closed"


def test_stop_twice_writes_one_stop(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, False)
    t.rec.stop("user_quit")
    t.rec.stop("user_quit")
    t.rec.shutdown("error")
    assert all_events(t.dir).count("RECORD_STOP") == 1
    assert sum(1 for m in t.messages if m.startswith("Recording stopped")) == 1


def test_sync_with_and_without_a_file(make_recorder):
    t = make_recorder()
    t.rec.sync()
    t.rec.apply_status(True, False)
    t.rec.event("key_down", "a")
    t.rec.sync()
    assert parse_log(t.rec.log_path)[-1]["event"] == "key_down"


# ---------------------------------------------------------------- video_time


def test_video_time_without_duration_takes_pauses_out(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, False)  # mono 0
    t.mono.now = 10.0
    t.rec.apply_status(True, True)
    t.mono.now = 25.0
    t.rec.apply_status(True, False)
    t.mono.now = 30.0
    t.rec.event("key_down", "a")
    rows = parse_log(t.rec.log_path)
    assert [(r["event"], r["video_time"]) for r in rows] == [
        ("RECORD_START", "0.000"),
        ("RECORD_PAUSE", "10.000"),
        ("RECORD_RESUME", "10.000"),
        ("key_down", "15.000"),
    ]
    assert t.rec.video_time() == pytest.approx(15.0)


def test_video_time_attached_mid_recording(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, False, duration_ms=42000)
    start = parse_log(t.rec.log_path)[0]
    assert start["video_time"] == "42.000"
    assert ("obs_duration_ms", "42000") in detail_pairs(start["detail"])


def test_video_time_reanchors_on_each_poll(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, False, duration_ms=0)
    t.mono.now = 10.0
    t.rec.apply_status(True, False, duration_ms=9500)  # OBS says the video is behind our clock
    t.rec.event("key_down", "a")
    t.mono.now = 11.0
    t.rec.event("key_up", "a")
    rows = parse_log(t.rec.log_path)
    assert [r["video_time"] for r in rows[1:]] == ["9.500", "10.500"]


def test_video_time_ignores_wall_clock_jumps(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, False)
    t.mono.advance(5.0)
    t.wall.advance(3600.0)
    t.rec.event("key_down", "a")
    row = parse_log(t.rec.log_path)[-1]
    assert row["video_time"] == "5.000"
    assert row["time"] == format_time(WALL_START + 3600.0)


def test_video_time_stop_while_paused_keeps_pause_moment(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, False)
    t.mono.now = 10.0
    t.rec.apply_status(True, True)
    t.mono.now = 20.0
    t.rec.stop("user_quit")
    assert parse_log(t.rec.log_path)[-1]["video_time"] == "10.000"


def test_video_time_frozen_while_paused_and_reanchored_on_resume(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, False, duration_ms=0)
    t.mono.now = 10.0
    t.rec.apply_status(True, True, duration_ms=10000)
    t.mono.now = 12.0
    t.rec.apply_status(True, True, duration_ms=99999)  # ignored while paused
    t.rec.note("still paused")
    t.mono.now = 25.0
    t.rec.apply_status(True, False, duration_ms=10200)
    t.mono.now = 30.0
    t.rec.event("key_down", "a")
    rows = parse_log(t.rec.log_path)
    assert [(r["event"], r["video_time"]) for r in rows[1:]] == [
        ("RECORD_PAUSE", "10.000"),
        ("NOTE", "10.000"),
        ("RECORD_RESUME", "10.000"),
        ("key_down", "15.200"),
    ]


def test_unusable_duration_falls_back_to_monotonic(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, False, duration_ms=True)
    t.mono.now = 3.0
    t.rec.apply_status(True, False, duration_ms="oops")
    t.rec.event("key_down", "a")
    rows = parse_log(t.rec.log_path)
    assert ("obs_duration_ms", "") in detail_pairs(rows[0]["detail"])
    assert rows[-1]["video_time"] == "3.000"


# ---------------------------------------------------------------- RECORD_START metadata


def test_start_metadata_order(make_recorder):
    meta = {
        "macrolens": "2.0.0",
        "obs": "localhost:4455",
        "log_format": 9,  # Recorder-owned keys from the caller are ignored
        "screen": "3840x1080",
        "screen_origin": "-1920,0",
        "dpi_scale": "1.25",
    }
    t = make_recorder(meta=meta)
    t.rec.apply_status(True, False, duration_ms=1500)
    pairs = detail_pairs(parse_log(t.rec.log_path)[0]["detail"])
    assert [k for k, _ in pairs] == [
        "macrolens",
        "log_format",
        "utc_offset",
        "obs",
        "obs_duration_ms",
        "screen",
        "screen_origin",
        "dpi_scale",
    ]
    values = dict(pairs)
    assert values["log_format"] == "2"
    assert values["utc_offset"] == utc_offset(WALL_START)
    assert values["obs_duration_ms"] == "1500"
    assert values["screen_origin"] == "-1920,0"


def test_start_metadata_minimal(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, False)
    pairs = detail_pairs(parse_log(t.rec.log_path)[0]["detail"])
    assert [k for k, _ in pairs] == ["log_format", "utc_offset", "obs_duration_ms"]
    assert dict(pairs)["obs_duration_ms"] == ""


def test_json_meta_matches_detail(make_recorder):
    t = make_recorder(fmt="json", meta={"macrolens": "2.0.0", "obs": "localhost:4455"})
    t.rec.apply_status(True, False, duration_ms=0)
    start = json.loads(read_lines(t.rec.log_path)[0])
    assert list(start)[:6] == list(COLUMNS)
    assert start["meta"]["log_format"] == 2
    assert {k: str(v) for k, v in start["meta"].items()} == dict(detail_pairs(start["detail"]))


# ---------------------------------------------------------------- locking


def test_stop_from_poll_does_not_deadlock(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, False)
    run_with_timeout(lambda: t.rec.apply_status(False, False))
    assert t.rec.state == "idle"


def test_shutdown_while_recording_does_not_deadlock(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, False)
    run_with_timeout(lambda: t.rec.shutdown("window_closed"))
    assert t.rec.shut_down


def test_shutdown_while_paused_does_not_deadlock(make_recorder):
    t = make_recorder()
    t.rec.apply_status(True, True)
    run_with_timeout(lambda: t.rec.shutdown("terminated"))
    assert parse_log(t.rec.log_path)[-1]["detail"] == "reason=terminated"


def test_concurrent_events_keep_every_line_whole(make_recorder):
    t = make_recorder()

    def typist():
        for i in range(300):
            t.rec.event("key_down", f"k{i}")

    threads = [threading.Thread(target=typist, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    for _ in range(20):
        t.wall.advance(1.0)
        t.rec.apply_status(True, False)
        t.rec.apply_status(True, True)
        t.rec.apply_status(True, False)
        t.rec.apply_status(False, False)
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert len(log_files(t.dir)) == 20  # every line is checked by the teardown sweep


# ---------------------------------------------------------------- file naming


def test_name_collision_never_appends(make_recorder):
    t = make_recorder()
    base = datetime.fromtimestamp(WALL_START).strftime("%Y-%m-%d_%H-%M-%S") + "_task_log"
    for _ in range(3):
        t.rec.apply_status(True, False)
        t.rec.apply_status(False, False)
    names = [os.path.basename(p) for p in log_files(t.dir)]
    assert names == [f"{base}.tsv", f"{base}_2.tsv", f"{base}_3.tsv"]


def test_existing_file_is_left_alone(make_recorder, tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    base = datetime.fromtimestamp(WALL_START).strftime("%Y-%m-%d_%H-%M-%S") + "_task_log"
    other = folder / f"{base}.jsonl"
    other.write_bytes(b"someone else's file\n")
    t = make_recorder(log_dir=folder, fmt="json")
    t.rec.apply_status(True, False)
    t.rec.stop("obs_stopped")
    assert t.rec.log_path.endswith(f"{base}_2.jsonl")
    assert other.read_bytes() == b"someone else's file\n"
    other.unlink()  # so the teardown sweep only sees v2 files


# ---------------------------------------------------------------- clean_field, time helpers


@pytest.mark.parametrize(
    "raw,clean",
    [
        ("\x03", "^C"),
        ("\x00", "^@"),
        ("\x1b", "^["),
        ("\x0b", "^K"),
        ("\x0c", "^L"),
        ("\x1c", "^\\"),
        ("\x7f", "^?"),
        ("\n", " "),
        ("\t", " "),
        ("\r", " "),
        ("\x85", " "),
        (" ", " "),
        (" ", " "),
        ("a\tb\r\nc", "a b  c"),
        (None, ""),
        (12, "12"),
        ("Привет — 日本語 😀", "Привет — 日本語 😀"),
    ],
)
def test_clean_field(raw, clean):
    assert clean_field(raw) == clean


def test_clean_field_never_splits_a_line():
    for code in list(range(0x3000)) + [0x2028, 0x2029, 0xFEFF, 0x1F600]:
        out = clean_field(chr(code))
        assert "\t" not in out
        assert len(("a" + out + "b").splitlines()) == 1, hex(code)


def test_format_time_and_utc_offset():
    stamp = format_time(WALL_START + 0.5)
    assert stamp == datetime.fromtimestamp(WALL_START + 0.5).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    assert len(stamp) == 23 and stamp[19] == "."
    for offset in (utc_offset(), utc_offset(WALL_START)):
        assert len(offset) == 6 and offset[0] in "+-" and offset[3] == ":"
        assert offset[1:3].isdigit() and offset[4:].isdigit()


# ---------------------------------------------------------------- sealing


def write_finished_log(folder, fmt="tsv"):
    """A real v2 log written by a Recorder, ended with obs_stopped. Returns its path."""
    wall, mono = FakeClock(WALL_START), FakeClock(0.0)
    rec = Recorder(
        str(folder),
        fmt,
        clock=wall,
        mono_clock=mono,
        window_title=lambda: "Editor",
        on_message=lambda m: None,
    )
    rec.apply_status(True, False)
    wall.advance(1.5)
    mono.advance(1.5)
    rec.event("key_down", "a")
    wall.advance(1.0)
    mono.advance(1.0)
    rec.event("key_up", "a")
    wall.advance(1.0)
    mono.advance(1.0)
    rec.stop("obs_stopped")
    return rec.log_path


def drop_last_line(path):
    """Simulate a crash: the log loses its RECORD_STOP line."""
    lines = read_lines(path)
    Path(path).write_bytes(("\n".join(lines[:-1]) + "\n").encode("utf-8"))


@pytest.mark.parametrize("fmt", ["tsv", "json"])
def test_seal_unfinished_log(tmp_path, fmt):
    folder = tmp_path / "logs"
    path = write_finished_log(folder, fmt)
    drop_last_line(path)
    before = parse_log(path)
    messages = []
    assert seal_unfinished_logs(str(folder), on_message=messages.append) == [path]
    rows = sweep_file(path)
    assert rows[:-1] == before
    assert rows[-1]["detail"] == "reason=unclean_exit"
    assert rows[-1]["time"] == before[-1]["time"]
    assert rows[-1]["video_time"] == before[-1]["video_time"] == "2.500"
    assert len(messages) == 1 and path in messages[0]


@pytest.mark.parametrize(
    "fmt,partial",
    [
        ("tsv", b"2026-01-01 00:00:0"),
        ("tsv", b"2026-01-01 00:00:03.000\t3.000\tkey_down\tEd\xe6\x97"),
        ("json", b'{"time": "2026'),
        ("json", b'{"time": "2026-01-01", "window": "\xe6\x97'),
    ],
)
def test_seal_truncates_a_cut_line(tmp_path, fmt, partial):
    folder = tmp_path / "logs"
    path = write_finished_log(folder, fmt)
    drop_last_line(path)
    before = parse_log(path)
    with open(path, "ab") as f:
        f.write(partial)
    assert seal_unfinished_logs(str(folder), on_message=lambda m: None) == [path]
    rows = sweep_file(path)
    assert rows[:-1] == before
    assert rows[-1]["detail"] == "reason=unclean_exit"


START_V1 = "2026-01-01 00:00:00.000\tRECORD_START\t\t\n2026-01-01 00:00:01.000\tkey_down\tEditor\ta\n"
START_V2 = TSV_HEADER + "\n" + "2026-01-01 00:00:00.000\t0.000\tRECORD_START\t\tlog_format=2\t\n"

UNTOUCHED = {
    "header_only": ("x_task_log.tsv", (TSV_HEADER + "\n").encode("utf-8")),
    "v1_tsv": ("x_task_log.tsv", START_V1.encode("utf-8")),
    "empty": ("x_task_log.tsv", b""),
    "no_newline": ("x_task_log.tsv", TSV_HEADER.encode("utf-8")),
    "bad_utf8": ("x_task_log.tsv", (TSV_HEADER + "\n").encode("utf-8") + b"\xff\xfe\t\n"),
    "short_line": ("x_task_log.tsv", (TSV_HEADER + "\nRECORD_START\n").encode("utf-8")),
    "json_v1_meta": ("x_task_log.jsonl", b'{"event": "RECORD_START", "meta": {"log_format": 1}}\n{"event": "x"}\n'),
    "json_no_meta": ("x_task_log.jsonl", b'{"event": "RECORD_START"}\n'),
    "json_no_start": ("x_task_log.jsonl", b'{"event": "key_down"}\n'),
    "json_not_json": ("x_task_log.jsonl", b"hello\n"),
    "json_list_line": ("x_task_log.jsonl", b'{"event": "RECORD_START", "meta": {"log_format": 2}}\n[1, 2]\n'),
    "not_a_log_name": ("notes.tsv", START_V2.encode("utf-8")),
}


@pytest.mark.parametrize("case", sorted(UNTOUCHED))
def test_seal_leaves_other_files_alone(tmp_path, case):
    name, data = UNTOUCHED[case]
    path = tmp_path / name
    path.write_bytes(data)
    assert seal_unfinished_logs(str(tmp_path), on_message=lambda m: None) == []
    assert path.read_bytes() == data


@pytest.mark.parametrize("fmt", ["tsv", "json"])
def test_seal_leaves_finished_log_alone(tmp_path, fmt):
    path = write_finished_log(tmp_path, fmt)
    data = Path(path).read_bytes()
    assert seal_unfinished_logs(str(tmp_path), on_message=lambda m: None) == []
    assert Path(path).read_bytes() == data


def test_seal_v2_start_only(tmp_path):
    path = tmp_path / "x_task_log.tsv"
    path.write_bytes(START_V2.encode("utf-8"))
    assert seal_unfinished_logs(str(tmp_path), on_message=lambda m: None) == [str(path)]
    rows = sweep_file(path)
    assert [r["event"] for r in rows] == ["RECORD_START", "RECORD_STOP"]
    assert rows[-1]["time"] == "2026-01-01 00:00:00.000"


def test_seal_twice_seals_once(tmp_path):
    path = write_finished_log(tmp_path)
    drop_last_line(path)
    assert seal_unfinished_logs(str(tmp_path), on_message=lambda m: None) == [path]
    data = Path(path).read_bytes()
    assert seal_unfinished_logs(str(tmp_path), on_message=lambda m: None) == []
    assert Path(path).read_bytes() == data


def test_seal_numbered_names(tmp_path):
    first = write_finished_log(tmp_path)
    second = write_finished_log(tmp_path)  # same second, so it's _2
    assert second.endswith("_task_log_2.tsv")
    drop_last_line(first)
    drop_last_line(second)
    assert seal_unfinished_logs(str(tmp_path), on_message=lambda m: None) == sorted([first, second])


def test_seal_missing_dir(tmp_path):
    assert seal_unfinished_logs(str(tmp_path / "nope"), on_message=lambda m: None) == []


# ---------------------------------------------------------------- the invariant sweep and contract drift

TITLE = "Editor\tmain.py — 日本語 \U0001f600\n"


def run_scripted_session(rec, wall, mono):
    """Every transition: idle, start, keys, pause, keys, resume, keys, stop, keys while idle, and so on."""

    def tick():
        wall.advance(1.0)
        mono.advance(1.0)

    def inputs():
        rec.event("key_down", "a")
        rec.event("key_down", "\n")
        rec.event("key_up", "\x03")
        rec.event("mouse_down", "left", "10,20")
        rec.event("mouse_up", "left", "-5,20")
        rec.event("scroll", "0,-1", "10,20")

    inputs()
    rec.apply_status(False, False)
    inputs()
    tick()
    rec.apply_status(True, False, duration_ms=0)
    inputs()
    tick()
    rec.apply_status(True, False, duration_ms=900)
    inputs()
    tick()
    rec.apply_status(True, True, duration_ms=2000)
    inputs()
    rec.note("paused for a password")
    tick()
    rec.apply_status(True, True)
    inputs()
    tick()
    rec.apply_status(True, False, duration_ms=2000)
    inputs()
    tick()
    rec.apply_status(False, False)  # obs_stopped
    inputs()
    tick()
    rec.apply_status(True, False)
    inputs()
    tick()
    rec.stop("obs_disconnected", last_confirmed=wall.now - 0.5)
    inputs()
    tick()
    rec.apply_status(True, False)
    inputs()
    tick()
    rec.shutdown("user_quit")
    inputs()
    rec.apply_status(True, False)
    inputs()


@pytest.mark.parametrize("fmt", ["tsv", "json"])
def test_invariant_sweep(make_recorder, fmt):
    t = make_recorder(fmt=fmt, window_title=lambda: TITLE)
    run_scripted_session(t.rec, t.wall, t.mono)
    files = log_files(t.dir)
    assert len(files) == 3
    stops = []
    for path in files:
        rows = sweep_file(path)
        stops.append(rows[-1]["detail"])
        inputs = [r for r in rows if r["event"] in INPUT_EVENT_KINDS]
        assert inputs, f"{path} has no input lines"
        assert all(r["window"] == clean_field(TITLE) for r in inputs)
    assert stops[0] == "reason=obs_stopped"
    assert stops[1].startswith("reason=obs_disconnected last_confirmed=")
    assert stops[2] == "reason=user_quit"
    key_details = [r["detail"] for r in parse_log(files[0]) if r["event"].startswith("key_")]
    assert key_details[:3] == ["a", " ", "^C"]


def test_contract_drift_tsv_vs_json(make_recorder, tmp_path):
    meta = {"macrolens": "2.0.0", "obs": "localhost:4455", "screen": "1920x1080"}
    tsv = make_recorder(log_dir=tmp_path / "tsv", fmt="tsv", meta=meta, window_title=lambda: TITLE)
    jsn = make_recorder(log_dir=tmp_path / "json", fmt="json", meta=meta, window_title=lambda: TITLE)
    run_scripted_session(tsv.rec, tsv.wall, tsv.mono)
    run_scripted_session(jsn.rec, jsn.wall, jsn.mono)
    tsv_files = log_files(tsv.dir)
    json_files = log_files(jsn.dir)
    assert [os.path.splitext(os.path.basename(p))[0] for p in tsv_files] == [
        os.path.splitext(os.path.basename(p))[0] for p in json_files
    ]
    tsv_rows = [row for path in tsv_files for row in parse_log(path)]
    json_rows = [row for path in json_files for row in parse_log(path)]
    assert tsv_rows == json_rows
    for path in json_files:
        start = json.loads(read_lines(path)[0])
        assert {k: str(v) for k, v in start["meta"].items()} == dict(detail_pairs(start["detail"]))


# ---------------------------------------------------------------- the instance lock


def test_instance_lock_cold_start(tmp_path):
    folder = tmp_path / "new" / "logs"
    release = acquire_instance_lock(str(folder), pid=111, pid_alive=lambda p: False)
    lock = folder / LOCK_FILE_NAME
    assert lock.read_text(encoding="ascii") == "111"
    release()
    assert not lock.exists()
    release()  # twice is fine


def test_instance_lock_default_pid(tmp_path):
    release = acquire_instance_lock(str(tmp_path), pid_alive=lambda p: False)
    assert (tmp_path / LOCK_FILE_NAME).read_text(encoding="ascii") == str(os.getpid())
    release()


def test_instance_lock_live_pid_refuses(tmp_path):
    lock = tmp_path / LOCK_FILE_NAME
    lock.write_text("222", encoding="ascii")
    with pytest.raises(AlreadyRunning) as info:
        acquire_instance_lock(str(tmp_path), pid=111, pid_alive=lambda p: p == 222)
    assert info.value.pid == 222
    assert str(info.value) == "Macrolens is already running for this log folder (PID 222). Close it first."
    assert lock.read_text(encoding="ascii") == "222"


@pytest.mark.parametrize("content", ["222", "", "not a pid", "-5"])
def test_instance_lock_stale_is_replaced(tmp_path, content):
    lock = tmp_path / LOCK_FILE_NAME
    lock.write_text(content, encoding="ascii")
    release = acquire_instance_lock(str(tmp_path), pid=111, pid_alive=lambda p: False)
    assert lock.read_text(encoding="ascii") == "111"
    release()
    assert not lock.exists()


def test_instance_lock_release_keeps_someone_elses_lock(tmp_path):
    release = acquire_instance_lock(str(tmp_path), pid=111, pid_alive=lambda p: False)
    lock = tmp_path / LOCK_FILE_NAME
    lock.write_text("333", encoding="ascii")
    release()
    assert lock.read_text(encoding="ascii") == "333"


def test_instance_lock_second_copy_refused_while_first_holds_it(tmp_path):
    release = acquire_instance_lock(str(tmp_path), pid=111, pid_alive=lambda p: False)
    with pytest.raises(AlreadyRunning):
        acquire_instance_lock(str(tmp_path), pid=222, pid_alive=lambda p: p == 111)
    release()
    release2 = acquire_instance_lock(str(tmp_path), pid=222, pid_alive=lambda p: False)
    release2()


def test_default_pid_alive():
    assert macrolens_log._pid_alive(os.getpid())
    assert not macrolens_log._pid_alive(0)
    assert not macrolens_log._pid_alive(-1)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not macrolens_log._pid_alive(proc.pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_permissions_private_to_the_user(make_recorder, tmp_path):
    folder = tmp_path / "logs"
    t = make_recorder(log_dir=folder)
    t.rec.apply_status(True, False)
    assert folder.stat().st_mode & 0o777 == 0o700
    assert os.stat(t.rec.log_path).st_mode & 0o777 == 0o600
    lock_dir = tmp_path / "locked"
    release = acquire_instance_lock(str(lock_dir), pid=111, pid_alive=lambda p: False)
    assert lock_dir.stat().st_mode & 0o777 == 0o700
    assert (lock_dir / LOCK_FILE_NAME).stat().st_mode & 0o777 == 0o600
    release()
