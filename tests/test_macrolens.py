"""Tests for macrolens.py: the command line, main(), the OBS session loop, listeners and close handling.

No pynput and no OBS: a scripted fake client, fake listeners, and a fake clock whose sleep only moves time.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import macrolens
from macrolens_log import COLUMNS, LOCK_FILE_NAME, LOG_NAME_RE, TSV_HEADER, Recorder, format_time
from macrolens_settings import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
WALL0 = 1_767_225_600.0  # about 1 Jan 2026

REC = {"outputActive": True, "outputPaused": False, "outputDuration": 1000}
PAUSED = {"outputActive": True, "outputPaused": True, "outputDuration": 1000}
IDLE = {"outputActive": False, "outputPaused": False, "outputDuration": 0}
NOT_READY = "not_ready"
EXIT = object()  # stands in for obswebsocket.events.ExitStarted
AUTH_MESSAGE = "Empty response to Identify, password may be inconnect."


class FakeClosed(Exception):
    """Stands in for websocket.WebSocketConnectionClosedException."""


class FakeTimeout(Exception):
    """Stands in for obswebsocket.exceptions.MessageTimeout."""


class FakeConnectionFailure(Exception):
    """Stands in for obswebsocket.exceptions.ConnectionFailure."""


LOST = (FakeClosed, OSError, FakeTimeout)


# ---------------------------------------------------------------- fakes


class FakeTime:
    """A monotonic clock that only moves when sleep() is called, plus a matching wall clock."""

    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.sleeps = 0
        self.on_sleep = on_sleep

    def clock(self):
        return self.now

    def wall(self):
        return WALL0 + self.now

    def sleep(self, seconds):
        self.now += seconds
        self.sleeps += 1
        if self.on_sleep is not None:
            self.on_sleep(self)


class FakeClient:
    """A scripted obsws. Each call() takes the next script item; an empty script ends the session."""

    def __init__(self, script=(), connect_error=None, connect_block=None):
        self.script = list(script)
        self.connect_error = connect_error
        self.connect_block = connect_block
        self.registered = []
        self.disconnects = 0
        self.calls = 0

    def connect(self):
        if self.connect_block is not None:
            self.connect_block.wait(10)
        if self.connect_error is not None:
            raise self.connect_error

    def register(self, func, event=None):
        self.registered.append((func, event))

    def fire_exit(self):
        for func, event in self.registered:
            if event is EXIT:
                func(SimpleNamespace(name="ExitStarted"))

    def call(self, request):
        self.calls += 1
        if not self.script:
            raise macrolens.Terminated()
        item = self.script.pop(0)
        if callable(item):
            item = item(self)
        if isinstance(item, BaseException):
            raise item
        if item == NOT_READY:
            return SimpleNamespace(status=False, datain={})
        return SimpleNamespace(status=True, datain=dict(item))

    def disconnect(self):
        self.disconnects += 1


class FakeFactory:
    """Hands out the given clients in order and records how it was called."""

    def __init__(self, clients):
        self.clients = list(clients)
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if not self.clients:
            raise macrolens.Terminated()
        return self.clients.pop(0)


class FakePorts:
    """port_open answers, in order; True once they run out."""

    def __init__(self, answers=()):
        self.answers = list(answers)

    def __call__(self, host, port):
        return self.answers.pop(0) if self.answers else True


class FakeListeners:
    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.checks = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def check(self):
        self.checks += 1


class SpyRecorder:
    """Records what run_session asks of the recorder."""

    state = "idle"

    def __init__(self):
        self.calls = []

    def apply_status(self, active, paused, duration_ms=None, stop_reason="obs_stopped"):
        self.calls.append(("apply", active, paused, duration_ms, stop_reason))

    def stop(self, reason, last_confirmed=None):
        self.calls.append(("stop", reason, last_confirmed))

    def shutdown(self, reason):
        self.calls.append(("shutdown", reason))

    def note(self, text):
        self.calls.append(("note", text))

    def sync(self):
        pass


class FakeListener:
    def __init__(self, kind):
        self.kind = kind
        self.alive = False
        self.stopped = False

    def start(self):
        self.alive = True

    def stop(self):
        self.stopped = True
        self.alive = False

    def is_alive(self):
        return self.alive


# ---------------------------------------------------------------- helpers


def make_settings(tmp_path, **overrides):
    values = {
        "host": "localhost",
        "port": 4455,
        "log_dir": str(tmp_path / "logs"),
        "format": "tsv",
        "reconnect_seconds": 5.0,
        "settings_path": None,
    }
    values.update(overrides)
    return Settings(**values)


def make_rec(tmp_path, t, messages=None):
    sink = messages if messages is not None else []
    return Recorder(
        str(tmp_path / "logs"),
        "tsv",
        clock=t.wall,
        mono_clock=t.clock,
        on_message=sink.append,
        window_title=lambda: "Editor",
    )


def log_files(log_dir):
    folder = Path(log_dir)
    if not folder.is_dir():
        return []
    return sorted(str(p) for p in folder.iterdir() if LOG_NAME_RE.search(p.name))


def rows(path):
    """A TSV log's rows as dicts; asserts the header, the final newline and six fields per line."""
    text = Path(path).read_bytes().decode("utf-8")
    assert text.endswith("\n")
    lines = text.split("\n")[:-1]
    assert lines[0] == TSV_HEADER
    out = []
    for line in lines[1:]:
        fields = line.split("\t")
        assert len(fields) == 6, line
        out.append(dict(zip(COLUMNS, fields, strict=True)))
    return out


def last_stop(path):
    last = rows(path)[-1]
    assert last["event"] == "RECORD_STOP"
    return last["detail"]


def run(tmp_path, clients, ports=(), recorder=None, time_=None, settings=None, **kwargs):
    """Run run_session against fakes and return what happened."""
    t = time_ if time_ is not None else FakeTime()
    messages = []
    rec = recorder if recorder is not None else make_rec(tmp_path, t, messages)
    factory = FakeFactory(clients)
    listeners = FakeListeners()
    options = {
        "sleep": t.sleep,
        "clock": t.clock,
        "on_message": messages.append,
        "port_open": FakePorts(ports),
        "get_record_status": lambda: "GetRecordStatus",
        "lost_errors": LOST,
        "exit_event_type": EXIT,
        "wall_clock": t.wall,
    }
    options.update(kwargs)
    code = macrolens.run_session(
        settings if settings is not None else make_settings(tmp_path), rec, "pw", factory, listeners, **options
    )
    return SimpleNamespace(
        code=code,
        messages=messages,
        rec=rec,
        factory=factory,
        listeners=listeners,
        time=t,
        files=log_files(tmp_path / "logs"),
    )


def after_exit(result):
    """A script item: OBS sends ExitStarted, then the call returns result."""

    def item(client):
        client.fire_exit()
        return result

    return item


def count(messages, text):
    return sum(1 for m in messages if text in m)


# ---------------------------------------------------------------- import and command line


def test_import_needs_no_runtime_packages():
    code = (
        "import macrolens, sys; "
        "assert 'pynput' not in sys.modules and 'obswebsocket' not in sys.modules "
        "and 'websocket' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


def test_help(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as info:
        macrolens.main(["--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert "settings" in out
    assert "OBS_PASSWORD" in out
    assert "every key" in out
    for flag in ("--host", "--port", "--log-dir", "--format", "--reconnect-seconds", "--settings", "--version"):
        assert flag in out


def test_version(capsys):
    with pytest.raises(SystemExit) as info:
        macrolens.main(["--version"])
    assert info.value.code == 0
    assert "2.0.0" in capsys.readouterr().out
    assert macrolens.__version__ == "2.0.0"


def test_parser_passes_only_given_flags():
    args = macrolens.build_parser().parse_args(["--port", "4456", "--format", "json"])
    assert vars(args) == {"port": 4456, "format": "json"}
    assert vars(macrolens.build_parser().parse_args([])) == {}


def _fail(*args, **kwargs):
    raise AssertionError("must not be called")


def test_main_ctrl_c_during_setup_returns_130(monkeypatch, tmp_path, capsys):
    def interrupt():
        raise KeyboardInterrupt

    monkeypatch.setattr(macrolens, "ensure_requirements", interrupt)
    log_dir = tmp_path / "logs"
    assert macrolens.main(["--log-dir", str(log_dir)]) == 130
    captured = capsys.readouterr()
    assert "Stopped." in captured.out
    assert "Traceback" not in captured.err
    assert not (log_dir / LOCK_FILE_NAME).exists()


def test_main_second_instance_returns_2(monkeypatch, tmp_path, capsys):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / LOCK_FILE_NAME).write_text(str(os.getpid()), encoding="ascii")
    monkeypatch.setattr(macrolens, "ensure_requirements", _fail)
    assert macrolens.main(["--log-dir", str(log_dir)]) == 2
    captured = capsys.readouterr()
    assert "already running" in captured.out + captured.err
    assert (log_dir / LOCK_FILE_NAME).exists()  # still the other copy's


def test_main_bad_settings_file_returns_2_before_requirements(monkeypatch, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text('{"port": 4455,,}', encoding="utf-8")
    monkeypatch.setattr(macrolens, "ensure_requirements", _fail)
    assert macrolens.main(["--settings", str(bad), "--log-dir", str(tmp_path / "logs")]) == 2
    err = capsys.readouterr().err
    assert "not valid JSON" in err
    assert "line 1" in err


def _patch_runtime(monkeypatch, listeners, run_session):
    monkeypatch.setattr(macrolens, "ensure_requirements", lambda: None)
    monkeypatch.setattr(macrolens, "get_obs_password", lambda: "pw")
    monkeypatch.setattr(macrolens, "_load_obs_client", lambda: "factory")
    monkeypatch.setattr(macrolens, "make_listeners", lambda recorder: listeners)
    monkeypatch.setattr(macrolens, "install_close_handlers", lambda recorder: (lambda: None))
    monkeypatch.setattr(macrolens, "_set_dpi_aware", lambda: None)
    monkeypatch.setattr(macrolens, "run_session", run_session)


def test_main_runs_a_session_and_cleans_up(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    listeners = FakeListeners()
    seen = {}

    def fake_run_session(settings, recorder, password, client_factory, listeners_arg):
        seen["settings"] = settings
        seen["recorder"] = recorder
        seen["password"] = password
        seen["client_factory"] = client_factory
        seen["listeners"] = listeners_arg
        assert (log_dir / LOCK_FILE_NAME).exists()
        return 0

    _patch_runtime(monkeypatch, listeners, fake_run_session)
    hook_before = threading.excepthook
    assert macrolens.main(["--log-dir", str(log_dir), "--format", "json"]) == 0
    assert seen["password"] == "pw"
    assert seen["client_factory"] == "factory"
    assert seen["listeners"] is listeners
    assert seen["settings"].log_dir == os.path.abspath(str(log_dir))
    assert seen["settings"].format == "json"
    assert listeners.started == 1 and listeners.stopped == 1
    assert seen["recorder"].shut_down
    assert not (log_dir / LOCK_FILE_NAME).exists()
    assert threading.excepthook is hook_before


def test_main_unexpected_error_ends_the_log_with_error(monkeypatch, tmp_path, capsys):
    log_dir = tmp_path / "logs"

    def broken_session(settings, recorder, *rest):
        recorder.apply_status(True, False)
        raise RuntimeError("bug")

    _patch_runtime(monkeypatch, FakeListeners(), broken_session)
    with pytest.raises(RuntimeError):
        macrolens.main(["--log-dir", str(log_dir)])
    files = log_files(log_dir)
    assert len(files) == 1
    assert last_stop(files[0]) == "reason=error"
    assert not (log_dir / LOCK_FILE_NAME).exists()
    assert "unexpected error (RuntimeError)" in capsys.readouterr().err


# ---------------------------------------------------------------- run_session: waiting and connecting


def test_waiting_printed_once_across_retries(tmp_path):
    r = run(tmp_path, [FakeClient()], ports=[False, False, False, True])
    assert r.code == 0
    waiting = [m for m in r.messages if m.startswith("Waiting for OBS")]
    assert len(waiting) == 1
    assert "localhost:4455" in waiting[0]
    assert r.time.now == pytest.approx(15.0)  # three waits of reconnect_seconds
    assert count(r.messages, "Connected to OBS at localhost:4455") == 1


def test_wrong_password_twice_returns_2(tmp_path):
    first = FakeClient(connect_error=FakeConnectionFailure(AUTH_MESSAGE))
    second = FakeClient(connect_error=FakeConnectionFailure(AUTH_MESSAGE))
    r = run(tmp_path, [first, second, FakeClient()])
    assert r.code == 2
    assert len(r.factory.calls) == 2
    assert count(r.messages, "refused the password") == 1
    assert count(r.messages, ".obs_password") == 1
    assert count(r.messages, "OBS_PASSWORD") == 1


def test_wrong_password_once_then_success_carries_on(tmp_path):
    r = run(tmp_path, [FakeClient(connect_error=FakeConnectionFailure(AUTH_MESSAGE)), FakeClient()])
    assert r.code == 0
    assert count(r.messages, "Connected to OBS") == 1
    assert count(r.messages, "refused the password") == 0


def test_auth_failures_must_be_in_a_row(tmp_path):
    clients = [
        FakeClient(connect_error=FakeConnectionFailure(AUTH_MESSAGE)),
        FakeClient(connect_error=ConnectionResetError(10054, "reset")),
        FakeClient(connect_error=FakeConnectionFailure(AUTH_MESSAGE)),
        FakeClient(),
    ]
    r = run(tmp_path, clients)
    assert r.code == 0
    assert len(r.factory.calls) == 4


def test_connect_reset_goes_back_to_waiting(tmp_path):
    r = run(tmp_path, [FakeClient(connect_error=ConnectionResetError(10054, "reset")), FakeClient()])
    assert r.code == 0
    assert len(r.factory.calls) == 2
    assert count(r.messages, "Connected to OBS") == 1


def test_other_connection_failure_goes_back_to_waiting(tmp_path):
    r = run(tmp_path, [FakeClient(connect_error=FakeConnectionFailure("Invalid Hello message.")), FakeClient()])
    assert r.code == 0
    assert len(r.factory.calls) == 2


def test_hung_connect_goes_back_to_waiting(tmp_path):
    release = threading.Event()
    hung = FakeClient(connect_block=release)
    try:
        r = run(tmp_path, [hung, FakeClient()], connect_timeout=0.3)
    finally:
        release.set()
    assert r.code == 0
    assert count(r.messages, "is not answering like OBS") == 1
    assert len(r.factory.calls) == 2
    assert count(r.messages, "Connected to OBS") == 1


@pytest.mark.parametrize("port", [4455, 4444])
def test_client_factory_always_gets_legacy_false(tmp_path, port):
    r = run(tmp_path, [FakeClient()], settings=make_settings(tmp_path, port=port))
    assert r.factory.calls[0] == (("localhost", port, "pw"), {"legacy": False, "timeout": 2})


# ---------------------------------------------------------------- run_session: recording and losing OBS


def test_lost_connection_ends_log_and_next_recording_gets_a_new_file(tmp_path):
    websocket = pytest.importorskip("websocket")
    pytest.importorskip("obswebsocket")
    first = FakeClient([REC, REC, websocket.WebSocketConnectionClosedException("closed")])
    second = FakeClient([REC])
    r = run(tmp_path, [first, second], lost_errors=None)  # the real default lost_errors
    assert r.code == 0
    assert len(r.files) == 2
    assert last_stop(r.files[0]) == f"reason=obs_disconnected last_confirmed={format_time(WALL0 + 1 / 30)}"
    assert last_stop(r.files[1]) == "reason=terminated"
    assert count(r.messages, "Lost the connection to OBS") == 1
    assert count(r.messages, "Connected to OBS") == 2
    assert first.disconnects >= 1


def test_message_timeout_counts_as_lost(tmp_path):
    r = run(tmp_path, [FakeClient([REC, FakeTimeout("No answer")])])
    assert r.code == 0
    assert len(r.files) == 1
    assert last_stop(r.files[0]).startswith("reason=obs_disconnected last_confirmed=")


def test_exit_started_then_stopped_is_obs_exited(tmp_path):
    client = FakeClient([REC, after_exit(IDLE)])
    r = run(tmp_path, [client])
    assert client.registered and client.registered[0][1] is EXIT
    assert len(r.files) == 1
    assert last_stop(r.files[0]) == "reason=obs_exited"


def test_exit_started_then_drop_is_obs_exited(tmp_path):
    r = run(tmp_path, [FakeClient([REC, after_exit(FakeClosed("gone"))])])
    assert len(r.files) == 1
    assert last_stop(r.files[0]).startswith("reason=obs_exited last_confirmed=")


def test_user_stops_recording_is_obs_stopped(tmp_path):
    r = run(tmp_path, [FakeClient([REC, REC, IDLE, IDLE])])
    assert len(r.files) == 1
    assert last_stop(r.files[0]) == "reason=obs_stopped"


def test_not_ready_briefly_holds_state(tmp_path):
    r = run(tmp_path, [FakeClient([REC] + [NOT_READY] * 30 + [REC])])
    assert len(r.files) == 1
    assert [row["event"] for row in rows(r.files[0])] == ["RECORD_START", "RECORD_STOP"]
    assert last_stop(r.files[0]) == "reason=terminated"
    assert count(r.messages, "Lost the connection") == 0


def test_not_ready_too_long_counts_as_lost(tmp_path):
    client = FakeClient([REC] + [NOT_READY] * 90)
    r = run(tmp_path, [client])
    assert len(r.files) == 1
    assert last_stop(r.files[0]) == f"reason=obs_disconnected last_confirmed={format_time(WALL0)}"
    assert count(r.messages, "Lost the connection") == 1
    assert client.calls <= 64  # gave up after about 2 seconds, not at the end of the script


def test_first_call_fails_last_confirmed_is_connect_time(tmp_path):
    spy = SpyRecorder()
    run(tmp_path, [FakeClient([FakeClosed("gone")])], ports=[False, True], recorder=spy)
    stops = [call for call in spy.calls if call[0] == "stop"]
    assert stops == [("stop", "obs_disconnected", WALL0 + 5.0)]
    assert spy.calls[-1] == ("shutdown", "terminated")


def test_output_duration_is_passed_to_the_recorder(tmp_path):
    spy = SpyRecorder()
    script = [dict(REC, outputDuration=1234), dict(REC, outputDuration=1300), {"outputActive": True}]
    run(tmp_path, [FakeClient(script)], recorder=spy)
    applies = [call for call in spy.calls if call[0] == "apply"]
    assert [call[3] for call in applies] == [1234, 1300, None]
    assert all(call[1] is True and call[2] is False and call[4] == "obs_stopped" for call in applies)


def test_paused_writes_no_keys(tmp_path):
    t = FakeTime()
    rec = make_rec(tmp_path, t)

    def key(name, result):
        def item(client):
            rec.event("key_down", name)
            return result

        return item

    script = [REC, key("a", REC), PAUSED, key("secret", PAUSED), REC, key("b", REC)]
    r = run(tmp_path, [FakeClient(script)], recorder=rec, time_=t)
    assert len(r.files) == 1
    logged = rows(r.files[0])
    assert [row["detail"] for row in logged if row["event"] == "key_down"] == ["a", "b"]
    events = [row["event"] for row in logged]
    assert events.index("RECORD_PAUSE") < events.index("RECORD_RESUME")


def test_housekeeping_about_once_a_second(tmp_path):
    r = run(tmp_path, [FakeClient([REC] * 91)])
    assert 2 <= r.listeners.checks <= 3


# ---------------------------------------------------------------- run_session: ending


def test_stop_event_ends_with_user_quit(tmp_path):
    stop = threading.Event()

    def set_stop(client):
        stop.set()
        return REC

    client = FakeClient([REC, set_stop, REC, REC])
    r = run(tmp_path, [client], stop_event=stop)
    assert r.code == 0
    assert last_stop(r.files[0]) == "reason=user_quit"
    assert client.disconnects == 1
    assert client.calls == 2


def test_terminated_from_sleep(tmp_path):
    def on_sleep(t):
        if t.sleeps >= 3:
            raise macrolens.Terminated()

    r = run(tmp_path, [FakeClient([REC] * 10)], time_=FakeTime(on_sleep))
    assert r.code == 0
    assert last_stop(r.files[0]) == "reason=terminated"


def test_ctrl_c_from_sleep_is_user_quit(tmp_path):
    def on_sleep(t):
        if t.sleeps >= 3:
            raise KeyboardInterrupt

    client = FakeClient([REC] * 10)
    r = run(tmp_path, [client], time_=FakeTime(on_sleep))
    assert r.code == 0
    assert last_stop(r.files[0]) == "reason=user_quit"
    assert client.disconnects == 1


# ---------------------------------------------------------------- thread exception filter


def _hook_args(thread, exc):
    return SimpleNamespace(exc_type=type(exc), exc_value=exc, exc_traceback=None, thread=thread)


def test_thread_exception_filter(monkeypatch):
    websocket = pytest.importorskip("websocket")
    seen = []

    def previous_hook(args):
        seen.append(args)

    class RecvThread:  # the name is what the filter matches
        pass

    class OtherThread:
        pass

    monkeypatch.setattr(threading, "excepthook", previous_hook)
    restore = macrolens.install_thread_exception_filter()
    try:
        for exc in (
            RuntimeError("cannot join current thread"),
            ConnectionResetError(10054, "reset"),
            websocket.WebSocketConnectionClosedException("closed"),
        ):
            threading.excepthook(_hook_args(RecvThread(), exc))
        assert seen == []
        threading.excepthook(_hook_args(OtherThread(), ValueError("x")))
        threading.excepthook(_hook_args(OtherThread(), OSError("y")))
        assert [type(args.exc_value) for args in seen] == [ValueError, OSError]
    finally:
        restore()
    assert threading.excepthook is previous_hook


# ---------------------------------------------------------------- listeners and callbacks


def test_listeners_check_restarts_a_dead_listener(tmp_path):
    t = FakeTime()
    rec = make_rec(tmp_path, t)
    made = []

    def maker(kind):
        def make():
            made.append(FakeListener(kind))
            return made[-1]

        return make

    listeners = macrolens.Listeners(rec, maker("keyboard"), maker("mouse"))
    listeners.start()
    assert [m.kind for m in made] == ["keyboard", "mouse"]
    rec.apply_status(True, False)
    made[0].alive = False  # the keyboard listener died
    listeners.check()
    assert [m.kind for m in made] == ["keyboard", "mouse", "keyboard"]
    assert made[2].alive
    listeners.check()
    assert len(made) == 3
    listeners.stop()
    assert made[1].stopped and made[2].stopped
    listeners.check()
    assert len(made) == 3
    rec.shutdown("user_quit")
    notes = [row["detail"] for row in rows(rec.log_path) if row["event"] == "NOTE"]
    assert notes == ["keyboard listener restarted"]


@pytest.mark.parametrize(
    "char, name",
    [("a", "a"), ("é", "é"), ("\n", "^J"), ("\t", "^I"), ("\r", "^M"), ("\x03", "^C"), ("\x7f", "^?"), ("", "")],
)
def test_key_name(char, name):
    assert macrolens.key_name(SimpleNamespace(char=char)) == name


def test_key_name_special_key():
    class SpecialKey:
        char = None

        def __str__(self):
            return "Key.space"

    assert macrolens.key_name(SpecialKey()) == "Key.space"


def test_ctrl_j_keeps_the_log_line_whole(tmp_path):
    t = FakeTime()
    rec = make_rec(tmp_path, t)
    rec.apply_status(True, False)
    callbacks = macrolens.make_callbacks(rec)
    callbacks["on_press"](SimpleNamespace(char="\n"))
    rec.shutdown("user_quit")
    keys = [row["detail"] for row in rows(rec.log_path) if row["event"] == "key_down"]
    assert keys == ["^J"]


class SpyEvents:
    def __init__(self, fail=False):
        self.events = []
        self.fail = fail

    def event(self, kind, detail="", position=""):
        if self.fail:
            raise ValueError("boom")
        self.events.append((kind, detail, position))


def test_callbacks_route_events():
    spy = SpyEvents()
    callbacks = macrolens.make_callbacks(spy)
    callbacks["on_press"](SimpleNamespace(char="a"))
    callbacks["on_release"](SimpleNamespace(char="a"))
    callbacks["on_click"](10, 20, SimpleNamespace(name="left"), True)
    callbacks["on_click"](10, 20, SimpleNamespace(name="left"), False)
    callbacks["on_scroll"](5, 6, 0, -1)
    assert spy.events == [
        ("key_down", "a", ""),
        ("key_up", "a", ""),
        ("mouse_down", "left", "10,20"),
        ("mouse_up", "left", "10,20"),
        ("scroll", "0,-1", "5,6"),
    ]


def test_callbacks_never_raise():
    callbacks = macrolens.make_callbacks(SpyEvents(fail=True))
    assert callbacks["on_press"](SimpleNamespace(char="a")) is None
    assert callbacks["on_release"](SimpleNamespace(char="a")) is None
    assert callbacks["on_click"](1, 2, object(), True) is None  # a button with no name
    assert callbacks["on_scroll"](1, 2, 0, 1) is None


# ---------------------------------------------------------------- console close handler (pure callback)


@pytest.mark.parametrize("event_code", [2, 5, 6])
def test_console_close_ends_the_log(tmp_path, event_code):
    t = FakeTime()
    rec = make_rec(tmp_path, t)
    rec.apply_status(True, False)
    assert macrolens._console_ctrl(event_code, rec) is True
    assert last_stop(rec.log_path) == "reason=window_closed"
    rec.apply_status(True, False)  # the main thread's late poll must not open a new file
    assert rec.state == "idle"
    assert len(log_files(tmp_path / "logs")) == 1


@pytest.mark.parametrize("event_code", [0, 1])
def test_console_ctrl_c_goes_to_python(tmp_path, event_code):
    t = FakeTime()
    rec = make_rec(tmp_path, t)
    rec.apply_status(True, False)
    assert macrolens._console_ctrl(event_code, rec) is False
    assert rec.state == "recording"
    rec.shutdown("user_quit")


# ---------------------------------------------------------------- shared-folder warning, port check


def test_warn_if_shared_git_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    out = []
    assert macrolens.warn_if_shared(str(repo / "logs"), on_message=out.append) is True
    assert len(out) == 1
    assert "git repo" in out[0]


def test_warn_if_shared_synced_folder():
    out = []
    path = os.path.join(os.sep, "macrolens-test-nowhere", "OneDrive", "logs")
    assert macrolens.warn_if_shared(path, on_message=out.append) is True
    assert len(out) == 1
    assert "synced folder" in out[0]


def test_warn_if_shared_plain_folder(tmp_path):
    out = []
    assert macrolens.warn_if_shared(str(tmp_path / "plain" / "logs"), on_message=out.append) is False
    assert out == []


def test_obs_port_open():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        assert macrolens.obs_port_open("127.0.0.1", port, timeout=2.0) is True
    finally:
        server.close()
    assert macrolens.obs_port_open("127.0.0.1", port, timeout=0.5) is False
