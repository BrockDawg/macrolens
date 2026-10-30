"""End-to-end: run_session with the real obs-websocket-py client against a fake OBS (tests/fake_obs.py).

Real time, real sockets, reconnect_seconds=1 and waits of at most 5 s, so each test stays under 10 s.
run_session runs in a daemon thread and ends through its stop_event; the fixtures end it even when an
assertion fails, so a failure can't hang CI. At teardown every log file in the test's folder must pass
the v2 invariant sweep.

pytest swaps threading.excepthook for each phase (setup, call, teardown), so Macrolens's thread filter is
installed in each phase that runs a session, and an unhandled thread exception fails the test outright.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import pytest
from fake_obs import FakeObs

import macrolens
from macrolens_log import COLUMNS, EVENT_KINDS, INPUT_EVENT_KINDS, LOG_NAME_RE, STOP_REASONS, TSV_HEADER, Recorder
from macrolens_settings import Settings

obswebsocket = pytest.importorskip("obswebsocket")
pytest.importorskip("websockets")

# A thread exception that reaches pytest (the filter missed it) fails the test instead of only warning.
pytestmark = pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")

PASSWORD = "hunter2"  # every test but the wrong-password one logs in with it, which proves the auth string
WAIT_SECONDS = 5.0
LOSS_SECONDS = 4.0
JOIN_SECONDS = 8.0


# ---------------------------------------------------------------- v2 helpers (copied from tests/test_log.py)


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


def sweep_file(path):
    """Assert the v2 invariants over every line: no input outside recording, one start, ends with a stop."""
    rows = parse_log(path)
    assert rows, f"{path} has no rows"
    assert rows[0]["event"] == "RECORD_START", f"{path}: first row is {rows[0]['event']}"
    assert rows[-1]["event"] == "RECORD_STOP", f"{path}: last row is {rows[-1]['event']}"
    assert sum(1 for row in rows if row["event"] == "RECORD_START") == 1
    state = "idle"
    last_video = 0.0
    for row in rows:
        kind = row["event"]
        assert kind in EVENT_KINDS, kind
        video = float(row["video_time"])
        assert video >= last_video, f"{path}: video_time steps back to {video} at {kind}"
        last_video = video
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


def live_rows(path):
    """The complete rows of a TSV log that may still be being written (a cut-off last line is skipped)."""
    try:
        data = Path(path).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.split("\n")[:-1]  # the last piece is "" or a line still being written
    if not lines or lines[0] != TSV_HEADER:
        return []
    rows = []
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) == 6:
            rows.append(dict(zip(COLUMNS, fields, strict=True)))
    return rows


def stop_reason(row):
    """The reason from a RECORD_STOP row's detail."""
    return row["detail"].split(" ")[0][len("reason="):]


def wait_until(predicate, timeout=WAIT_SECONDS, interval=0.05):
    """Poll predicate until it returns something truthy or timeout passes. Returns its last value."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


# ---------------------------------------------------------------- fakes and harness


class FakeListeners:
    """Stands in for the pynput listeners: run_session only calls check()."""

    def __init__(self):
        self.checks = 0

    def start(self):
        pass

    def stop(self):
        pass

    def check(self):
        self.checks += 1


class SessionThread:
    """run_session on a daemon thread, with its result or exception kept for the test."""

    def __init__(self, kwargs):
        self.result = None
        self.error = None
        self.finished = threading.Event()
        self._kwargs = kwargs
        self.thread = threading.Thread(target=self._run, name="macrolens-session", daemon=True)

    def _run(self):
        try:
            self.result = macrolens.run_session(**self._kwargs)
        except Exception as err:  # kept for the test to report
            self.error = err
        finally:
            self.finished.set()

    def start(self):
        self.thread.start()

    def join(self, timeout):
        self.thread.join(timeout)
        return not self.thread.is_alive()


class Harness:
    """One test's fake OBS servers, its run_session thread and its log folder."""

    def __init__(self, log_dir, activate_filter):
        self.log_dir = log_dir
        self.activate_filter = activate_filter
        self.messages = []
        self.stop_event = threading.Event()
        self.fakes = []
        self.session = None
        self.recorder = None

    def fake(self, password=PASSWORD, **state):
        """Start a fake OBS on a free port, already in the given recording state."""
        obs = FakeObs(password=password)
        self.fakes.append(obs)
        if state:
            obs.set(**state)
        return obs.start()

    def start_session(self, obs, password=PASSWORD):
        """Run run_session against obs on a daemon thread, with the real obsws client."""
        self.activate_filter()  # the call phase has its own excepthook; filter it like main() does
        self.recorder = Recorder(
            str(self.log_dir),
            "tsv",
            meta={"macrolens": macrolens.__version__, "obs": f"{obs.host}:{obs.port}"},
            on_message=self.messages.append,
        )
        settings = Settings(
            host=obs.host,
            port=obs.port,
            log_dir=str(self.log_dir),
            format="tsv",
            reconnect_seconds=1.0,
            settings_path=None,
        )
        self.session = SessionThread(
            {
                "settings": settings,
                "recorder": self.recorder,
                "password": password,
                "client_factory": obswebsocket.obsws,
                "listeners": FakeListeners(),
                "on_message": self.messages.append,
                "stop_event": self.stop_event,
                "connect_timeout": 5.0,
            }
        )
        self.session.start()
        return self.session

    def files(self):
        return log_files(self.log_dir)

    def rows(self, index):
        """The complete rows of the index-th log file so far (empty when it doesn't exist yet)."""
        files = self.files()
        return live_rows(files[index]) if len(files) > index else []

    def has_event(self, index, event):
        return any(row["event"] == event for row in self.rows(index))

    def stopped_row(self, index):
        """The RECORD_STOP row ending the index-th file, or None while it's still open."""
        rows = self.rows(index)
        if rows and rows[-1]["event"] == "RECORD_STOP":
            return rows[-1]
        return None

    def wait(self, predicate, what, timeout=WAIT_SECONDS):
        value = wait_until(predicate, timeout)
        if not value:
            session = self.session
            error = None if session is None else session.error
            pytest.fail(
                f"Timed out after {timeout} s waiting for {what}. Files: {self.files()}. "
                f"Messages: {self.messages}. Session error: {error!r}"
            )
        return value

    def wait_for_event(self, index, event, timeout=WAIT_SECONDS):
        self.wait(lambda: self.has_event(index, event), f"{event} in log file {index + 1}", timeout)

    def wait_for_stop(self, index, timeout=WAIT_SECONDS):
        return self.wait(lambda: self.stopped_row(index), f"RECORD_STOP in log file {index + 1}", timeout)

    def end_session(self, timeout=JOIN_SECONDS):
        """Set the stop event and wait for run_session to return. True when it did."""
        self.stop_event.set()
        if self.session is None:
            return True
        return self.session.join(timeout)

    def close(self):
        """End the session, then every fake. Raises when the session thread would not end."""
        ended = self.end_session()
        for obs in self.fakes:
            obs.stop()
        assert ended, f"run_session did not return within {JOIN_SECONDS} s of its stop_event. {self.messages}"
        assert self.session is None or self.session.error is None, f"run_session raised {self.session.error!r}"


@pytest.fixture
def thread_filter():
    """Macrolens's RecvThread filter and quiet library logger, as main() sets them; restored at teardown.

    Yields activate(), which installs the filter again in a later phase (pytest swaps the hook per phase).
    """
    library_logger = logging.getLogger("obswebsocket")
    old_level = library_logger.level
    library_logger.setLevel(logging.CRITICAL)
    restores = [macrolens.install_thread_exception_filter()]

    def activate():
        restores.append(macrolens.install_thread_exception_filter())

    yield activate
    while restores:
        restores.pop()()
    library_logger.setLevel(old_level)


@pytest.fixture
def harness(tmp_path, thread_filter):
    """The test's harness. At teardown it ends the session and the fakes, then sweeps every log file."""
    h = Harness(tmp_path / "logs", thread_filter)
    yield h
    thread_filter()  # teardown phase: filter the library's threads while the connections close
    h.close()
    for path in log_files(h.log_dir):
        sweep_file(path)


# ---------------------------------------------------------------- the tests


def test_record_then_stop_in_obs(harness):
    obs = harness.fake(active=True)
    harness.start_session(obs)
    harness.wait_for_event(0, "RECORD_START")

    harness.recorder.event("key_down", "a")
    harness.recorder.event("key_up", "a")
    harness.recorder.event("mouse_down", "left", "10,20")
    harness.recorder.event("scroll", "0,-1", "10,20")
    obs.set(active=False)
    stop = harness.wait_for_stop(0)

    path = harness.files()[0]
    assert read_lines(path)[0] == TSV_HEADER
    rows = parse_log(path)
    assert rows[0]["event"] == "RECORD_START"
    assert "log_format=2" in rows[0]["detail"].split(" ")
    assert f"obs={obs.host}:{obs.port}" in rows[0]["detail"].split(" ")
    inputs = [(row["event"], row["detail"], row["position"]) for row in rows[1:-1]]
    assert inputs == [
        ("key_down", "a", ""),
        ("key_up", "a", ""),
        ("mouse_down", "left", "10,20"),
        ("scroll", "0,-1", "10,20"),
    ]
    assert stop["detail"] == "reason=obs_stopped"
    assert any(m.startswith("Connected to OBS") for m in harness.messages)
    assert len(harness.files()) == 1


def _assert_lost_then_back(harness, obs, capfd, drop):
    """Recording; drop the connection the given way; the log ends obs_disconnected; OBS returns, a new file."""
    harness.start_session(obs)
    harness.wait_for_event(0, "RECORD_START")
    harness.recorder.event("key_down", "x")

    drop()
    stop = harness.wait_for_stop(0, timeout=LOSS_SECONDS)
    assert stop_reason(stop) == "obs_disconnected"
    assert "last_confirmed=" in stop["detail"]
    harness.wait(
        lambda: any(m.startswith("Lost the connection to OBS") for m in harness.messages), "the lost message"
    )

    obs.start()  # the same port, still recording
    harness.wait_for_event(1, "RECORD_START", timeout=LOSS_SECONDS)
    assert len(harness.files()) == 2
    assert stop_reason(parse_log(harness.files()[0])[-1]) == "obs_disconnected"

    err = capfd.readouterr().err
    assert "Traceback" not in err, err
    assert "Exception in thread" not in err, err


def test_obs_crash_ends_log_and_reconnects(harness, capfd):
    obs = harness.fake(active=True)
    _assert_lost_then_back(harness, obs, capfd, obs.abort)


def test_obs_clean_close_ends_log_and_reconnects(harness, capfd):
    obs = harness.fake(active=True)
    _assert_lost_then_back(harness, obs, capfd, obs.stop)


def test_obs_exit_started_then_close_is_obs_exited(harness):
    obs = harness.fake(active=True)
    harness.start_session(obs)
    harness.wait_for_event(0, "RECORD_START")

    assert obs.send_exit_started() == 1
    time.sleep(0.3)  # OBS takes a moment between announcing its exit and closing
    obs.stop()
    stop = harness.wait_for_stop(0, timeout=LOSS_SECONDS)
    assert stop_reason(stop) == "obs_exited"


def test_wrong_password_returns_2(harness):
    obs = harness.fake(password="right-password", active=True)
    session = harness.start_session(obs, password="wrong-password")

    assert session.join(LOSS_SECONDS), f"run_session still running after {LOSS_SECONDS} s: {harness.messages}"
    assert session.error is None, repr(session.error)
    assert session.result == 2
    assert obs.auth_failures >= 2
    assert obs.identified_count == 0
    assert any("refused the password" in m for m in harness.messages), harness.messages
    assert harness.files() == []


def test_pause_resume_follows_obs_duration(harness):
    t_set = time.monotonic()
    obs = harness.fake(active=True, duration_ms=42_000)  # attached 42 s into the recording
    harness.start_session(obs)
    harness.wait_for_event(0, "RECORD_START")
    start_bound = 42.0 + (time.monotonic() - t_set)

    time.sleep(0.3)
    harness.recorder.event("key_down", "before")
    obs.set(paused=True)
    pause_ms = obs.duration_ms()
    harness.wait_for_event(0, "RECORD_PAUSE")
    harness.recorder.event("key_down", "secret")  # paused: must not be logged
    time.sleep(0.6)
    obs.set(paused=False)
    harness.wait_for_event(0, "RECORD_RESUME")
    harness.recorder.event("key_down", "after")
    time.sleep(0.3)
    obs.set(active=False)
    end_ms = obs.duration_ms()
    harness.wait_for_stop(0)

    rows = parse_log(harness.files()[0])
    by_event = {}
    for row in rows:
        by_event.setdefault(row["event"], []).append(row)
    keys = [row["detail"] for row in by_event["key_down"]]
    assert keys == ["before", "after"]

    start_v = float(by_event["RECORD_START"][0]["video_time"])
    pause_v = float(by_event["RECORD_PAUSE"][0]["video_time"])
    resume_v = float(by_event["RECORD_RESUME"][0]["video_time"])
    stop_v = float(by_event["RECORD_STOP"][0]["video_time"])
    assert 42.0 <= start_v <= start_bound, (start_v, start_bound)
    assert abs(pause_v - pause_ms / 1000) < 0.5, (pause_v, pause_ms)
    assert by_event["RECORD_RESUME"][0]["video_time"] == by_event["RECORD_PAUSE"][0]["video_time"]
    assert abs(stop_v - end_ms / 1000) < 0.5, (stop_v, end_ms)
    # The 0.6 s pause is taken out: the video advanced about 0.3 s from the resume to the stop.
    assert stop_v - resume_v < 0.3 + 0.5, (resume_v, stop_v)
    assert stop_reason(by_event["RECORD_STOP"][0]) == "obs_stopped"


def test_stop_event_ends_with_user_quit(harness):
    obs = harness.fake(active=True)
    session = harness.start_session(obs)
    harness.wait_for_event(0, "RECORD_START")
    harness.recorder.event("key_down", "q")

    assert harness.end_session(), f"run_session did not return: {harness.messages}"
    assert session.error is None, repr(session.error)
    assert session.result == 0
    rows = parse_log(harness.files()[0])
    assert rows[-1]["event"] == "RECORD_STOP"
    assert rows[-1]["detail"] == "reason=user_quit"
    assert harness.recorder.shut_down
