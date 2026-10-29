# Original work: https://github.com/PINTO0309/obs-key-logger (Apache License 2.0, see LICENSE).
# MODIFIED by Brock (BrockDawg), 2026-10-29. Changes from the original:
#   - Keys, mouse clicks and scrolls are recorded only while OBS is recording
#     (the original buffered keys at all times and wrote them out when recording began).
#   - Key releases, mouse clicks, scrolls and the active window title are logged.
#   - Esc is logged like any other key. Stop the program with Ctrl+C or by closing its window.
#   - Logs go to a logs/ folder beside this file, one tab-separated line per event.
#   - A RECORD_START line and a RECORD_STOP line carry wall-clock times to line the log up with the video.
#   - Renamed from obs-key-logger.py to macrolens.py. The MP4 player, C# logger and build files were removed.
#   - Offers to install the requirements when they are missing, and asks for the OBS password when none is stored.
#   - 2.0: honours OBS pause (nothing is logged while the recording is paused).
#   - 2.0: waits for OBS and reconnects when OBS restarts.
#   - 2.0: every log ends with a RECORD_STOP line that gives the reason.
#   - 2.0: --help, --version and a settings file (macrolens.settings.json).
#   - 2.0: log format v2 with a header row, a video_time column and optional JSON Lines output.
"""Macrolens: log keys, clicks and scrolls while OBS records, so an AI can turn the video into an automation.

Importing this module needs only the standard library and the two Macrolens modules beside it.
OBS (obs-websocket-py) and pynput are imported inside main(), after the requirements check.
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import functools
import getpass
import importlib
import importlib.util
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from macrolens_log import AlreadyRunning, Recorder, acquire_instance_lock, seal_unfinished_logs
from macrolens_settings import SETTINGS_FILE_NAME, SettingsError, load_settings

__version__ = "2.0.0"

HERE = os.path.dirname(os.path.abspath(__file__))
REQUIREMENTS_FILE = os.path.join(HERE, "requirements.txt")
PASSWORD_FILE = os.path.join(HERE, ".obs_password")  # git-ignored; an empty file means "OBS has no password"
DEFAULT_LOG_DIR = os.path.join(HERE, "logs")  # git-ignored by this repo's .gitignore

# import name -> pip package, for the check below
REQUIRED_MODULES = {"pynput": "pynput", "obswebsocket": "obs-websocket-py"}

POLL_INTERVAL = 1 / 30  # seconds between GetRecordStatus polls
REQUEST_TIMEOUT = 2  # seconds the library waits for an answer (its default is 60)
NOT_READY_LIMIT = 2.0  # seconds of "not ready" answers before the connection counts as lost
HOUSEKEEPING_SECONDS = 1.0  # how often to fsync the log and check the listeners
WAIT_SLICE = 0.2  # longest single sleep while a stop_event is watched
CONNECT_TIMEOUT = 10.0  # seconds a connect may take before the port counts as "not OBS"
AUTH_FAILED_PREFIX = "Empty response to Identify"  # obs-websocket-py's message when OBS refuses the password

# Console control events (SetConsoleCtrlHandler): Ctrl+C, Ctrl+Break, window closed, logoff, shutdown.
CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CLOSE_EVENTS = (2, 5, 6)

SYNCED_FOLDER_MARKERS = ("onedrive", "dropbox", "google drive", "icloud")

# Keeps the ctypes console handler alive; a collected handler would crash the process when called.
_CONSOLE_HANDLERS = []

EPILOG = """\
Settings file: macrolens.settings.json beside macrolens.py, or the file
given with --settings. Command-line flags win over the file, and the file
wins over the defaults.

Log folder: logs beside macrolens.py, unless log_dir or --log-dir says
otherwise.

OBS password: the OBS_PASSWORD environment variable, else the saved
.obs_password file beside macrolens.py, else Macrolens asks once. The
password never goes in the settings file.

Privacy: Macrolens records every key you press while OBS records,
passwords included. Pause OBS before typing anything private.

Press Ctrl+C to quit.
"""

DESCRIPTION = """\
Log the keys, clicks and scrolls you make while OBS records, with the
focused window, so an AI can turn the recording into an automation.
"""


class Terminated(Exception):
    """Raised by the SIGTERM/SIGHUP handler, so the session ends with reason=terminated."""


class _StopRequested(Exception):
    """The caller's stop_event was set: end the session like Ctrl+C."""


# ---------------------------------------------------------------- requirements and password


def ask_yes_no(question, default=True):
    """Ask on the console. Returns the default when there is no console to ask on."""
    if not sys.stdin or not sys.stdin.isatty():
        return default
    answer = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not answer else answer.startswith("y")


def ensure_requirements():
    """If a required package is missing, offer to install requirements.txt, then carry on."""
    missing = [pkg for mod, pkg in REQUIRED_MODULES.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    print(f"Macrolens needs these packages, which are not installed: {', '.join(missing)}")
    interactive = bool(sys.stdin and sys.stdin.isatty())
    if not interactive or not ask_yes_no("Install them now with pip?"):
        sys.exit(f"Install them with: {sys.executable} -m pip install -r requirements.txt")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS_FILE])
    if result.returncode != 0:
        sys.exit("pip could not install the packages (see the messages above). Fix that and run Macrolens again.")
    importlib.invalidate_caches()
    print("Installed.\n")


def get_obs_password():
    """The OBS password: env OBS_PASSWORD, else the stored one, else ask once and offer to store it."""
    if "OBS_PASSWORD" in os.environ:
        return os.environ["OBS_PASSWORD"]
    if os.path.exists(PASSWORD_FILE):
        with open(PASSWORD_FILE, encoding="utf-8") as f:
            return f.read().strip("\r\n")
    if not (sys.stdin and sys.stdin.isatty()):
        return ""  # nothing to ask on; try OBS without a password
    password = getpass.getpass("OBS WebSocket password (press Enter if OBS has none): ")
    if ask_yes_no("Save it on this computer so Macrolens does not ask again?"):
        with open(PASSWORD_FILE, "w", encoding="utf-8") as f:
            f.write(password)
        print(f"Saved to {PASSWORD_FILE}. Delete that file to be asked again.")
    return password


def password_guidance(host, port):
    """What to tell the user when OBS refuses the password."""
    return (
        f"OBS at {host}:{port} refused the password.\n"
        f"Delete {PASSWORD_FILE} (or fix the OBS_PASSWORD environment variable) and run Macrolens again."
    )


# ---------------------------------------------------------------- screen, window, input callbacks


def active_window_title():
    """Title of the focused window. Windows only, empty elsewhere. The Recorder cleans tabs and breaks."""
    if sys.platform != "win32":
        return ""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return ""


def screen_meta():
    """RECORD_START screen facts (Windows only, best effort): virtual screen size, origin and DPI scale."""
    if sys.platform != "win32":
        return {}
    meta = {}
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return {}
    try:
        width, height = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)  # SM_CXVIRTUALSCREEN, SM_CY...
        if width > 0 and height > 0:
            meta["screen"] = f"{width}x{height}"
    except Exception:
        pass
    try:
        left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)  # SM_XVIRTUALSCREEN, SM_Y...
        meta["screen_origin"] = f"{left},{top}"
    except Exception:
        pass
    try:
        dpi = user32.GetDpiForSystem()
        if dpi:
            meta["dpi_scale"] = f"{dpi / 96:.2f}"
    except Exception:
        pass
    return meta


def _set_dpi_aware():
    """Windows: per-monitor DPI aware (v2), so screen metrics are physical pixels like pynput's coordinates."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


def key_name(key):
    """The logged name of a key: its character, ^X for a control character (Ctrl+J is ^J), else str(key)."""
    char = getattr(key, "char", None)
    if char is None:
        return str(key)
    if len(char) == 1 and (char < " " or char == "\x7f"):
        return "^?" if char == "\x7f" else "^" + chr(ord(char) + 64)
    return char


def make_callbacks(recorder):
    """The four pynput callbacks. None of them can raise, so a bad event never stops a listener."""

    def on_press(key, *_extra):
        try:
            recorder.event("key_down", key_name(key))
        except Exception:
            pass

    def on_release(key, *_extra):
        try:
            recorder.event("key_up", key_name(key))
        except Exception:
            pass

    def on_click(x, y, button, pressed, *_extra):
        try:
            recorder.event("mouse_down" if pressed else "mouse_up", button.name, f"{x},{y}")
        except Exception:
            pass

    def on_scroll(x, y, dx, dy, *_extra):
        try:
            recorder.event("scroll", f"{dx},{dy}", f"{x},{y}")
        except Exception:
            pass

    return {"on_press": on_press, "on_release": on_release, "on_click": on_click, "on_scroll": on_scroll}


def _is_alive(listener):
    is_alive = getattr(listener, "is_alive", None)
    if callable(is_alive):
        return bool(is_alive())
    return bool(getattr(listener, "running", False))


class Listeners:
    """The keyboard and mouse listeners. check() replaces one that died, since pynput can't restart it."""

    def __init__(self, recorder, make_keyboard, make_mouse):
        self._recorder = recorder
        self._makers = {"keyboard": make_keyboard, "mouse": make_mouse}
        self._live = {}
        self._stopped = False

    def start(self):
        """Create and start both listeners."""
        for name, make in self._makers.items():
            listener = make()
            listener.start()
            self._live[name] = listener

    def check(self):
        """Re-create and start any listener whose thread has ended, and say so in the log."""
        if self._stopped:
            return
        for name, listener in list(self._live.items()):
            if _is_alive(listener):
                continue
            try:
                replacement = self._makers[name]()
                replacement.start()
            except Exception:
                continue  # try again at the next check
            self._live[name] = replacement
            self._recorder.note(f"{name} listener restarted")

    def stop(self):
        """Stop both listeners. check() does nothing afterwards."""
        self._stopped = True
        for listener in self._live.values():
            try:
                listener.stop()
            except Exception:
                pass


def make_listeners(recorder):
    """Real pynput listeners wired to the recorder. Imports pynput, so call it after ensure_requirements()."""
    from pynput import keyboard, mouse

    callbacks = make_callbacks(recorder)

    def make_keyboard():
        return keyboard.Listener(on_press=callbacks["on_press"], on_release=callbacks["on_release"])

    def make_mouse():
        return mouse.Listener(on_click=callbacks["on_click"], on_scroll=callbacks["on_scroll"])

    return Listeners(recorder, make_keyboard, make_mouse)


def _load_obs_client():
    """The obs-websocket-py client class. Imported here so the module stays import-safe."""
    from obswebsocket import obsws

    return obsws


# ---------------------------------------------------------------- safety checks


def _inside_git_repo(path):
    current = path
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def warn_if_shared(log_dir, on_message=print):
    """Print one warning when the log folder is in a git repo or a synced folder. A warning, not a refusal.

    The default logs folder beside macrolens.py is in this repo but git-ignored, so it doesn't count as a repo.
    """
    try:
        path = os.path.abspath(log_dir)
        places = []
        is_default = os.path.normcase(path) == os.path.normcase(DEFAULT_LOG_DIR)
        if not is_default and _inside_git_repo(path):
            places.append("a git repo")
        lowered = path.lower()
        if any(marker in lowered for marker in SYNCED_FOLDER_MARKERS):
            places.append("a synced folder (OneDrive, Dropbox, Google Drive or iCloud)")
    except Exception:
        return False
    if not places:
        return False
    on_message(
        f"Warning: your log folder is inside {' and '.join(places)}, so keystroke logs could leave this "
        f"computer: {path}"
    )
    return True


def obs_port_open(host, port, timeout=1.0) -> bool:
    """True when something accepts a TCP connection on host:port. Never raises."""
    try:
        conn = socket.create_connection((host, port), timeout=timeout)
    except Exception:
        return False
    try:
        conn.close()
    except Exception:
        pass
    return True


# ---------------------------------------------------------------- thread tracebacks and close handlers


def install_thread_exception_filter():
    """Silence obs-websocket-py's receive thread, which raises when OBS closes or crashes. Returns restore().

    The poll loop notices the loss through call() within the request timeout, so nothing is lost.
    """
    previous = threading.excepthook

    def hook(args):
        if type(args.thread).__name__ == "RecvThread":
            return
        previous(args)

    threading.excepthook = hook

    def restore():
        threading.excepthook = previous

    return restore


def _console_ctrl(event_code, recorder) -> bool:
    """The console control callback. Close, logoff and shutdown end the log; Ctrl+C goes to Python."""
    if event_code in CLOSE_EVENTS:
        try:
            recorder.shutdown("window_closed")
        except Exception:
            pass
        return True
    return False


def _raise_terminated(signum, frame):
    """SIGTERM/SIGHUP: only raise, never touch the Recorder (the main thread may hold its lock)."""
    raise Terminated()


def _shutdown_at_exit(recorder):
    try:
        recorder.shutdown("error")
    except Exception:
        pass


def _remove_console_handler(kernel32, handler):
    kernel32.SetConsoleCtrlHandler(handler, False)
    if handler in _CONSOLE_HANDLERS:
        _CONSOLE_HANDLERS.remove(handler)


def _install_console_handler(recorder, undo):
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    prototype = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def handler(event_code):
        return bool(_console_ctrl(event_code, recorder))

    c_handler = prototype(handler)
    if not kernel32.SetConsoleCtrlHandler(c_handler, True):
        return  # no console (a service or a CI runner): carry on without it
    _CONSOLE_HANDLERS.append(c_handler)
    undo.append(functools.partial(_remove_console_handler, kernel32, c_handler))


def install_close_handlers(recorder):
    """End the log cleanly when the console closes (Windows) or on SIGTERM/SIGHUP. Returns uninstall()."""
    undo = []
    if sys.platform == "win32":
        try:
            _install_console_handler(recorder, undo)
        except Exception:
            pass
    else:
        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                previous = signal.signal(signum, _raise_terminated)
            except (ValueError, OSError):
                continue  # not the main thread, or not allowed here
            if previous is None:
                previous = signal.SIG_DFL
            undo.append(functools.partial(signal.signal, signum, previous))
    atexit.register(_shutdown_at_exit, recorder)
    undo.append(functools.partial(atexit.unregister, _shutdown_at_exit))

    def uninstall():
        while undo:
            step = undo.pop()
            try:
                step()
            except Exception:
                pass

    return uninstall


# ---------------------------------------------------------------- the OBS session


def _quiet_disconnect(client):
    if client is None:
        return
    try:
        client.disconnect()
    except Exception:
        pass


def _is_auth_failure(error):
    return error is not None and str(error).startswith(AUTH_FAILED_PREFIX)


def _make_exit_flag():
    """An Event set by OBS's ExitStarted, and the callback that sets it."""
    flag = threading.Event()

    def on_exit(_event=None):
        flag.set()

    return flag, on_exit


class _ConnectAttempt:
    """client.connect() on a worker thread, so a port that never answers can't hang the loop."""

    def __init__(self, client):
        self.client = client
        self.error = None
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._abandoned = False

    def run(self):
        try:
            self.client.connect()
        except Exception as err:  # handed to the main thread
            self.error = err
        with self._lock:
            self.done.set()
            abandoned = self._abandoned
        if abandoned and self.error is None:
            _quiet_disconnect(self.client)  # it connected after we gave up on it

    def abandon(self):
        """Give up on the attempt. False when it had already finished."""
        with self._lock:
            if self.done.is_set():
                return False
            self._abandoned = True
            return True


def _connect_with_timeout(client, timeout, check_stop):
    """Returns ("connected", None), ("error", exception) or ("hung", None)."""
    attempt = _ConnectAttempt(client)
    thread = threading.Thread(target=attempt.run, name="macrolens-connect", daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while not attempt.done.wait(max(0.0, min(WAIT_SLICE, deadline - time.monotonic()))):
        if time.monotonic() >= deadline and attempt.abandon():
            return "hung", None
        try:
            check_stop()
        except BaseException:
            attempt.abandon()
            raise
    if attempt.error is not None:
        return "error", attempt.error
    return "connected", None


def run_session(
    settings,
    recorder,
    password,
    client_factory,
    listeners,
    sleep=time.sleep,
    clock=time.monotonic,
    on_message=print,
    port_open=obs_port_open,
    stop_event=None,
    get_record_status=None,
    lost_errors=None,
    exit_event_type=None,
    wall_clock=time.time,
    connect_timeout=CONNECT_TIMEOUT,
) -> int:
    """Connect to OBS, poll its recording state, and reconnect when it goes away. Returns the exit code.

    0 when the user quits (Ctrl+C, stop_event) or the process is terminated; 2 when OBS refuses the password.
    """
    if get_record_status is None:
        from obswebsocket import requests as obs_requests

        get_record_status = obs_requests.GetRecordStatus
    if lost_errors is None:
        import websocket
        from obswebsocket import exceptions as obs_exceptions

        lost_errors = (
            websocket.WebSocketConnectionClosedException,
            OSError,
            obs_exceptions.MessageTimeout,
            obs_exceptions.ConnectionFailure,
        )
    if exit_event_type is None:
        from obswebsocket import events as obs_events

        exit_event_type = obs_events.ExitStarted

    host, port = settings.host, settings.port
    where = f"{host}:{port}"

    def check_stop():
        if stop_event is not None and stop_event.is_set():
            raise _StopRequested()

    def wait(seconds):
        """Sleep, in slices when a stop_event is watched, so setting it ends the session promptly."""
        if stop_event is None:
            sleep(seconds)
            return
        remaining = seconds
        while True:
            check_stop()
            if remaining <= 1e-9:
                return
            step = min(WAIT_SLICE, remaining)
            sleep(step)
            remaining -= step

    client = None
    said_waiting = False
    said_hung = False
    auth_failures = 0
    try:
        while True:
            check_stop()
            if not port_open(host, port):
                if not said_waiting:
                    on_message(
                        f"Waiting for OBS at {where}. Open OBS and turn on Tools > WebSocket Server Settings > "
                        "Enable WebSocket server. (Ctrl+C to quit)"
                    )
                    said_waiting = True
                auth_failures = 0
                wait(settings.reconnect_seconds)
                continue

            # Always legacy=False: the library silently switches to the v4 protocol on port 4444.
            client = client_factory(host, port, password, legacy=False, timeout=REQUEST_TIMEOUT)
            outcome, error = _connect_with_timeout(client, connect_timeout, check_stop)
            if outcome != "connected":
                failed, client = client, None
                if outcome == "hung":
                    auth_failures = 0
                    if not said_hung:
                        on_message(f"Something on {where} is not answering like OBS.")
                        said_hung = True
                else:
                    _quiet_disconnect(failed)
                    if _is_auth_failure(error):
                        auth_failures += 1
                        if auth_failures >= 2:
                            on_message(password_guidance(host, port))
                            return 2
                    else:
                        auth_failures = 0  # OBS is starting or closing
                wait(settings.reconnect_seconds)
                continue

            said_waiting = False
            said_hung = False
            auth_failures = 0
            exiting, on_exit = _make_exit_flag()
            client.register(on_exit, exit_event_type)
            on_message(f"Connected to OBS at {where}. Press Ctrl+C to quit.")
            last_ok = clock()
            last_ok_wall = wall_clock()
            next_housekeeping = last_ok + HOUSEKEEPING_SECONDS
            while True:
                check_stop()
                try:
                    status = client.call(get_record_status())
                except lost_errors:
                    break
                now = clock()
                if getattr(status, "status", None):
                    datain = getattr(status, "datain", None) or {}
                    recorder.apply_status(
                        bool(datain.get("outputActive")),
                        bool(datain.get("outputPaused")),
                        duration_ms=datain.get("outputDuration"),
                        stop_reason="obs_exited" if exiting.is_set() else "obs_stopped",
                    )
                    last_ok = now
                    last_ok_wall = wall_clock()
                elif now - last_ok > NOT_READY_LIMIT:
                    break  # "not ready" for too long: treat it as lost
                if now >= next_housekeeping:
                    recorder.sync()
                    listeners.check()
                    next_housekeeping = now + HOUSEKEEPING_SECONDS
                wait(POLL_INTERVAL)

            recorder.stop("obs_exited" if exiting.is_set() else "obs_disconnected", last_confirmed=last_ok_wall)
            _quiet_disconnect(client)
            client = None
            on_message("Lost the connection to OBS. Waiting for it to come back.")
            said_waiting = True  # that line already says we're waiting
            wait(settings.reconnect_seconds)
    except (KeyboardInterrupt, _StopRequested):
        recorder.shutdown("user_quit")
        _quiet_disconnect(client)
        return 0
    except Terminated:
        recorder.shutdown("terminated")
        _quiet_disconnect(client)
        return 0


# ---------------------------------------------------------------- command line


def build_parser():
    """The command-line parser. Only flags the user gives reach load_settings (argparse.SUPPRESS)."""
    parser = argparse.ArgumentParser(
        prog="macrolens",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        argument_default=argparse.SUPPRESS,
    )
    parser.add_argument("--host", help="OBS WebSocket host (default localhost)")
    parser.add_argument("--port", type=int, help="OBS WebSocket port (default 4455)")
    parser.add_argument("--log-dir", dest="log_dir", help="log folder (default: logs beside macrolens.py)")
    parser.add_argument("--format", choices=("tsv", "json"), help="log format: tsv (default) or json (JSON Lines)")
    parser.add_argument(
        "--reconnect-seconds",
        dest="reconnect_seconds",
        type=float,
        help="seconds between tries to reach OBS (default 5)",
    )
    parser.add_argument("--settings", metavar="PATH", help=f"settings file to use instead of {SETTINGS_FILE_NAME}")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return parser


def _reconfigure_streams():
    """A non-ASCII path must not crash a redirected cp1252 console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def _quietly(fn, *args):
    try:
        fn(*args)
    except Exception:
        pass


def _run(argv):
    _reconfigure_streams()
    args = build_parser().parse_args(argv)  # --help and --version exit here, before any package check
    try:
        settings = load_settings(vars(args), HERE, os.getcwd())
    except SettingsError as err:
        print(err, file=sys.stderr)
        return 2
    warn_if_shared(settings.log_dir)
    try:
        release = acquire_instance_lock(settings.log_dir)
    except AlreadyRunning as err:
        print(err, file=sys.stderr)
        return 2
    except OSError as err:
        print(f"Could not use the log folder {settings.log_dir} ({err.strerror or err}).", file=sys.stderr)
        return 2

    recorder = None
    listeners = None
    uninstall = None
    restore_filter = None
    try:
        seal_unfinished_logs(settings.log_dir)
        ensure_requirements()
        client_factory = _load_obs_client()
        _set_dpi_aware()  # before screen_meta() and before the listeners start
        logging.getLogger("obswebsocket").setLevel(logging.CRITICAL)
        restore_filter = install_thread_exception_filter()
        password = get_obs_password()
        meta = {"macrolens": __version__, "obs": f"{settings.host}:{settings.port}", **screen_meta()}
        recorder = Recorder(settings.log_dir, settings.format, meta=meta, window_title=active_window_title)
        listeners = make_listeners(recorder)
        listeners.start()
        uninstall = install_close_handlers(recorder)
        return run_session(settings, recorder, password, client_factory, listeners)
    except Exception as err:
        if recorder is not None:
            _quietly(recorder.shutdown, "error")
        print(f"Macrolens stopped on an unexpected error ({type(err).__name__}).", file=sys.stderr)
        raise
    finally:
        if listeners is not None:
            _quietly(listeners.stop)
        if uninstall is not None:
            _quietly(uninstall)
        if recorder is not None:
            _quietly(recorder.shutdown, "user_quit")  # a no-op unless a log is still open
        if restore_filter is not None:
            _quietly(restore_filter)
        release()


def main(argv=None) -> int:
    """Run Macrolens. Ctrl+C anywhere, even at a prompt, prints "Stopped." and returns 130."""
    try:
        return _run(argv)
    except KeyboardInterrupt:
        print("Stopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
