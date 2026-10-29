"""Macrolens log writer: log format v2 (TSV or JSON Lines), the Recorder state machine,
sealing of logs a crash left unfinished, and the one-copy-per-log-folder lock.

Stdlib only, so it imports and tests without OBS, pynput or a real keyboard.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import sys
import threading
import time
from datetime import datetime

LOG_FORMAT_VERSION = 2

EVENT_KINDS = (
    "RECORD_START",
    "RECORD_PAUSE",
    "RECORD_RESUME",
    "RECORD_STOP",
    "NOTE",
    "key_down",
    "key_up",
    "mouse_down",
    "mouse_up",
    "scroll",
)

# The kinds the input listeners write. Only these are dropped while paused or idle.
INPUT_EVENT_KINDS = ("key_down", "key_up", "mouse_down", "mouse_up", "scroll")

STOP_REASONS = (
    "obs_stopped",
    "obs_exited",
    "obs_disconnected",
    "user_quit",
    "window_closed",
    "terminated",
    "error",
    "unclean_exit",
)

TSV_HEADER = "time\tvideo_time\tevent\twindow\tdetail\tposition"
COLUMNS = tuple(TSV_HEADER.split("\t"))

# --format value -> file extension.
FORMAT_EXTENSIONS = {"tsv": "tsv", "json": "jsonl"}

LOCK_FILE_NAME = ".macrolens.lock"

# Log file names this module writes: <stamp>_task_log.tsv, <stamp>_task_log_2.jsonl and so on.
LOG_NAME_RE = re.compile(r"_task_log(?:_\d+)?\.(?:tsv|jsonl)$")

_O_BINARY = getattr(os, "O_BINARY", 0)  # Windows: no CRT newline translation on the raw fd.

# Characters that would split a line for a reader: tab and the line breaks str.splitlines() honours.
# The last two below are literal U+2028 and U+2029 (they look like spaces in most editors).
_SPACE_CHARS = frozenset("\t\r\n\x85  ")


def clean_field(value) -> str:
    """Make one field safe for a single log line: breaks become spaces, control chars become ^X."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    out = []
    for ch in text:
        if ch in _SPACE_CHARS:
            out.append(" ")
        elif ch < " ":
            out.append("^" + chr(ord(ch) + 64))
        elif ch == "\x7f":
            out.append("^?")
        else:
            out.append(ch)
    return "".join(out)


def format_time(ts: float) -> str:
    """Local wall clock as YYYY-MM-DD HH:MM:SS.mmm."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def utc_offset(ts: float | None = None) -> str:
    """The local UTC offset as +HH:MM or -HH:MM (now, or at ts when given)."""
    moment = datetime.now() if ts is None else datetime.fromtimestamp(ts)
    offset = moment.astimezone().utcoffset()
    total = int(offset.total_seconds()) if offset is not None else 0
    sign = "-" if total < 0 else "+"
    hours, minutes = divmod(abs(total) // 60, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def _check_reason(reason):
    if reason not in STOP_REASONS:
        raise ValueError(f"Unknown stop reason {reason!r}. Allowed: {', '.join(STOP_REASONS)}")


def _as_ms(value):
    """OBS's outputDuration as a float, or None when it is missing or not a usable number."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _no_title():
    return ""


class Recorder:
    """Writes one log file per OBS recording. States: idle, recording, paused."""

    def __init__(
        self,
        log_dir,
        fmt="tsv",
        meta=None,
        clock=time.time,
        mono_clock=time.monotonic,
        window_title=_no_title,
        on_message=print,
    ):
        if fmt not in FORMAT_EXTENSIONS:
            raise ValueError(f"Unknown log format {fmt!r}. Use tsv or json.")
        self._log_dir = log_dir
        self._fmt = fmt
        self._meta = dict(meta or {})
        self._clock = clock
        self._mono = mono_clock
        self._window_title = window_title
        self._on_message = on_message
        self._lock = threading.Lock()
        self._state = "idle"
        self._shut_down = False
        self._file = None
        self._path = None
        # video_time = anchor_video + (mono now - anchor_mono) while recording; frozen while paused.
        self._anchor_video = 0.0
        self._anchor_mono = 0.0
        self._frozen = 0.0

    # ---- public API: each method takes the lock once and never calls another public method ----

    @property
    def state(self) -> str:
        return self._state

    @property
    def log_path(self):
        """The current log file, or the last one written."""
        return self._path

    @property
    def shut_down(self) -> bool:
        return self._shut_down

    def video_time(self) -> float:
        """Seconds into the OBS video right now (frozen while paused)."""
        with self._lock:
            return self._video_locked(self._mono())

    def apply_status(self, active, paused, duration_ms=None, stop_reason="obs_stopped"):
        """Apply one GetRecordStatus result. The whole state machine. Does nothing after shutdown()."""
        _check_reason(stop_reason)
        messages = []
        with self._lock:
            if self._shut_down:
                return
            now = self._mono()
            ms = _as_ms(duration_ms)
            if self._state == "idle":
                if active:
                    messages.append(self._open_locked(now, ms))
                    if paused:
                        self._pause_locked(now)
            elif not active:
                messages.append(self._stop_locked(stop_reason, None))
            elif self._state == "recording":
                if ms is not None:
                    self._anchor_locked(now, ms)
                if paused:
                    self._pause_locked(now)
            elif not paused:  # paused, and OBS has resumed
                self._resume_locked(now, ms)
        self._say(messages)

    def event(self, kind, detail="", position=""):
        """Write one input event. Does nothing unless OBS is recording (not paused)."""
        if kind not in INPUT_EVENT_KINDS:
            raise ValueError(f"Unknown input event {kind!r}")
        with self._lock:
            if self._state != "recording" or self._file is None:
                return
            try:
                window = self._window_title()
            except Exception:
                window = ""
            self._write_locked(kind, self._video_locked(self._mono()), window, detail, position)

    def note(self, text):
        """Write a NOTE line. Only while a log is open (recording or paused)."""
        with self._lock:
            if self._state not in ("recording", "paused") or self._file is None:
                return
            self._write_locked("NOTE", self._video_locked(self._mono()), "", text, "")

    def stop(self, reason, last_confirmed=None):
        """End the open log with RECORD_STOP, fsync and close it. Idempotent."""
        _check_reason(reason)
        with self._lock:
            message = self._stop_locked(reason, last_confirmed)
        self._say([message])

    def shutdown(self, reason):
        """Stop, then refuse any later start: the process is closing."""
        _check_reason(reason)
        with self._lock:
            try:
                message = self._stop_locked(reason, None)
            finally:
                self._shut_down = True
        self._say([message])

    def sync(self):
        """Flush and fsync the open log, so a power cut loses at most about a second."""
        with self._lock:
            if self._file is not None:
                self._file.flush()
                os.fsync(self._file.fileno())

    # ---- private helpers: the caller holds self._lock ----

    def _say(self, messages):
        for message in messages:
            if message:
                self._on_message(message)

    def _video_locked(self, now):
        if self._state != "recording":
            return self._frozen
        return max(0.0, self._anchor_video + (now - self._anchor_mono))

    def _anchor_locked(self, now, ms):
        self._anchor_video = ms / 1000.0
        self._anchor_mono = now

    def _format_line(self, ts, video, kind, window, detail, position, meta=None):
        values = [
            format_time(ts),
            "%.3f" % video,
            kind,
            clean_field(window),
            clean_field(detail),
            clean_field(position),
        ]
        if self._fmt == "tsv":
            return "\t".join(values)
        obj = dict(zip(COLUMNS, values, strict=True))
        obj["video_time"] = float(values[1])
        if meta is not None:
            obj["meta"] = meta
        return json.dumps(obj, ensure_ascii=False)

    def _write_locked(self, kind, video, window="", detail="", position=""):
        line = self._format_line(self._clock(), video, kind, window, detail, position)
        self._file.write(line + "\n")
        self._file.flush()

    def _start_meta_locked(self, ts, ms):
        """RECORD_START metadata pairs, in the order the log format fixes."""
        extra = dict(self._meta)
        for owned in ("log_format", "utc_offset", "obs_duration_ms"):
            extra.pop(owned, None)
        pairs = []
        if "macrolens" in extra:
            pairs.append(("macrolens", extra.pop("macrolens")))
        pairs.append(("log_format", LOG_FORMAT_VERSION))
        pairs.append(("utc_offset", utc_offset(ts)))
        if "obs" in extra:
            pairs.append(("obs", extra.pop("obs")))
        pairs.append(("obs_duration_ms", "" if ms is None else str(int(round(ms)))))
        pairs.extend(extra.items())
        return [(clean_field(k).replace(" ", "_"), v) for k, v in pairs]

    def _create_file_locked(self, ts):
        """Create a new log file, never reusing a name: _2, _3 and so on when one exists."""
        os.makedirs(self._log_dir, mode=0o700, exist_ok=True)
        base = datetime.fromtimestamp(ts).strftime("%Y-%m-%d_%H-%M-%S") + "_task_log"
        ext = FORMAT_EXTENSIONS[self._fmt]
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY
        for n in range(1, 10000):
            suffix = "" if n == 1 else f"_{n}"
            path = os.path.join(self._log_dir, f"{base}{suffix}.{ext}")
            try:
                fd = os.open(path, flags, 0o600)
            except FileExistsError:
                continue
            try:
                handle = io.open(fd, "w", encoding="utf-8", errors="replace", newline="\n")
            except BaseException:
                os.close(fd)
                raise
            return path, handle
        raise FileExistsError(f"Too many logs named {base} in {self._log_dir}")

    def _open_locked(self, now, ms):
        ts = self._clock()
        path, handle = self._create_file_locked(ts)
        video = 0.0 if ms is None else ms / 1000.0
        pairs = self._start_meta_locked(ts, ms)
        detail = " ".join(f"{k}={clean_field(v)}" for k, v in pairs)
        meta = {k: (v if k == "log_format" else clean_field(v)) for k, v in pairs}
        start = self._format_line(ts, video, "RECORD_START", "", detail, "", meta=meta)
        text = (TSV_HEADER + "\n" + start + "\n") if self._fmt == "tsv" else (start + "\n")
        try:
            handle.write(text)  # header and RECORD_START in one write
            handle.flush()
        except BaseException:
            handle.close()
            raise
        self._file = handle
        self._path = path
        self._anchor_video = video
        self._anchor_mono = now
        self._frozen = video
        self._state = "recording"
        return f"Recording started. Log: {path}"

    def _pause_locked(self, now):
        self._frozen = self._video_locked(now)
        self._state = "paused"
        self._write_locked("RECORD_PAUSE", self._frozen)

    def _resume_locked(self, now, ms):
        self._write_locked("RECORD_RESUME", self._frozen)
        self._anchor_video = self._frozen if ms is None else ms / 1000.0
        self._anchor_mono = now
        self._state = "recording"

    def _stop_locked(self, reason, last_confirmed):
        if self._file is None:
            self._state = "idle"
            return None
        video = self._video_locked(self._mono())
        detail = f"reason={reason}"
        if last_confirmed is not None:
            detail += f" last_confirmed={format_time(last_confirmed)}"
        handle = self._file
        try:
            self._write_locked("RECORD_STOP", video, "", detail, "")
            os.fsync(handle.fileno())
        finally:
            self._file = None
            self._state = "idle"
            self._frozen = video
            try:
                handle.close()
            except OSError:
                pass
        return f"Recording stopped ({reason}). Log: {self._path}"


# ---- sealing logs a crash left without RECORD_STOP ----


def _seal_one(path):
    """Seal one v2 log that has no RECORD_STOP. Returns True when it was sealed."""
    with open(path, "rb") as f:
        data = f.read()
    end = data.rfind(b"\n")
    if end == -1:
        return False  # empty, or no complete line at all
    lines = data[: end + 1].decode("utf-8").split("\n")[:-1]
    cut_partial = end + 1 < len(data)
    if path.endswith(".tsv"):
        if len(lines) < 2 or lines[0] != TSV_HEADER:
            return False  # v1 (no header) or header only
        first = lines[1].split("\t")
        last = lines[-1].split("\t")
        if len(first) != 6 or len(last) != 6:
            raise ValueError("not a v2 line")
        if first[2] != "RECORD_START" or last[2] == "RECORD_STOP":
            return False
        stop = "\t".join([last[0], last[1], "RECORD_STOP", "", "reason=unclean_exit", ""])
    else:
        head = json.loads(lines[0])
        if not isinstance(head, dict) or head.get("event") != "RECORD_START":
            return False
        meta = head.get("meta")
        if not isinstance(meta, dict) or meta.get("log_format") != LOG_FORMAT_VERSION:
            return False
        last = json.loads(lines[-1])
        if not isinstance(last, dict):
            raise ValueError("not a v2 line")
        if last.get("event") == "RECORD_STOP":
            return False
        stop = json.dumps(
            {
                "time": last.get("time", ""),
                "video_time": last.get("video_time", 0.0),
                "event": "RECORD_STOP",
                "window": "",
                "detail": "reason=unclean_exit",
                "position": "",
            },
            ensure_ascii=False,
        )
    with open(path, "r+b") as f:
        if cut_partial:
            f.truncate(end + 1)  # the line cut short mid-write
        f.seek(0, os.SEEK_END)
        f.write((stop + "\n").encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    return True


def seal_unfinished_logs(log_dir, on_message=print) -> list:
    """Give every v2 log left without RECORD_STOP (a crash or kill) a RECORD_STOP reason=unclean_exit.

    Returns the sealed paths. Never raises: a file it can't read or parse is left as it is.
    """
    try:
        names = sorted(os.listdir(log_dir))
    except OSError:
        return []
    sealed = []
    for name in names:
        if not LOG_NAME_RE.search(name):
            continue
        path = os.path.join(log_dir, name)
        try:
            if not os.path.isfile(path) or not _seal_one(path):
                continue
        except (OSError, ValueError, TypeError, KeyError):
            continue
        sealed.append(path)
        try:
            on_message(f"Sealed an unfinished log (it had no RECORD_STOP): {path}")
        except Exception:
            pass
    return sealed


# ---- one Macrolens per log folder ----


class AlreadyRunning(Exception):
    """Another live Macrolens holds this log folder's lock."""

    def __init__(self, pid):
        self.pid = pid
        super().__init__(f"Macrolens is already running for this log folder (PID {pid}). Close it first.")


def _pid_alive_windows(pid):
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # access denied: it exists, it just isn't ours
    try:
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == 259  # STILL_ACTIVE
        return True
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid) -> bool:
    """True when a process with this PID is running."""
    if not isinstance(pid, int) or pid <= 0 or pid > 0xFFFFFFFF:
        return False
    if sys.platform == "win32":
        try:
            return _pid_alive_windows(pid)
        except (OSError, AttributeError, ValueError, TypeError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lock_pid(path):
    """The PID in a lock file, or None when it's missing, empty or not a PID."""
    try:
        with open(path, "rb") as f:
            pid = int(f.read(64).decode("ascii").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _make_release(path, pid):
    released = []

    def release():
        """Remove the lock, once, and only while it is still ours."""
        if released:
            return
        released.append(True)
        if _read_lock_pid(path) == pid:
            try:
                os.remove(path)
            except OSError:
                pass

    return release


def acquire_instance_lock(log_dir, pid=None, pid_alive=None):
    """Take <log_dir>/.macrolens.lock. Raises AlreadyRunning when a live copy holds it.

    Returns release(), which is idempotent. A lock left by a dead process is replaced.
    """
    if pid is None:
        pid = os.getpid()
    if pid_alive is None:
        pid_alive = _pid_alive
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    path = os.path.join(log_dir, LOCK_FILE_NAME)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY
    for _attempt in range(3):
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            other = _read_lock_pid(path)
            if other is not None and pid_alive(other):
                raise AlreadyRunning(other) from None
            try:
                os.remove(path)  # stale: its process is gone
            except FileNotFoundError:
                pass
            continue
        try:
            os.write(fd, str(pid).encode("ascii"))
        finally:
            os.close(fd)
        return _make_release(path, pid)
    raise AlreadyRunning(_read_lock_pid(path) or 0)
