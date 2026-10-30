"""A fake OBS for the integration tests: an obs-websocket v5 server on 127.0.0.1, built on websockets 13.1.

It speaks just enough of the protocol for obs-websocket-py 1.0's real client: Hello (op 0), Identify (op 1)
with the optional password challenge, Identified (op 2), Request (op 6) and RequestResponse (op 7) for
GetRecordStatus, and the ExitStarted event (op 5). stop() closes like OBS exiting (a close frame);
abort() drops the sockets like OBS crashing (no close frame). start() again reuses the same port.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import struct
import sys
import threading
import time

HOST = "127.0.0.1"
BIND_RETRY_SECONDS = 2.0  # the Windows runner can be slow to free a port
RECV_SLICE = 0.2  # a handler re-checks whether the fake is still running this often
AUTH_FAILED_CODE = 4009  # WebSocketCloseCode::AuthenticationFailed
NOT_READY_CODE = 207  # RequestStatus::NotReady
SUCCESS_CODE = 100  # RequestStatus::Success

# websockets logs a failed opening handshake (Macrolens's port probe is one) with a traceback.
# Keep that off the console: these records go nowhere.
_QUIET_LOGGER = logging.getLogger("fake_obs.websockets")
_QUIET_LOGGER.addHandler(logging.NullHandler())
_QUIET_LOGGER.propagate = False
_QUIET_LOGGER.setLevel(logging.CRITICAL)


def obs_auth_string(password, salt, challenge):
    """The Identify authentication string, computed exactly as obsws._build_auth_string does."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest())
    return base64.b64encode(hashlib.sha256(secret + challenge.encode("utf-8")).digest()).decode("utf-8")


def _timecode(ms):
    seconds, millis = divmod(int(ms), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _serve_quietly(server):
    """Server.serve_forever, minus the select() error a closed socket can raise on Windows."""
    try:
        server.serve_forever()
    except Exception:
        pass


def _drop_socket(sock):
    """Close a connection's socket with no close frame, as a crash would (a reset where the OS allows)."""
    try:
        linger = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
    except OSError:
        pass
    try:
        sock.shutdown(socket.SHUT_RDWR)  # wakes a recv() blocked on it; close() alone doesn't on Linux
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class FakeObs:
    """A fake OBS WebSocket server. Port 0 picks a free port on the first start()."""

    def __init__(self, password=None, port=0, host=HOST):
        self.password = password  # None means OBS has authentication turned off
        self.host = host
        self._port = port
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self._running = False
        self._conns = set()
        self._identified = set()
        # What GetRecordStatus answers. outputDuration is kept up to date on every read.
        self.state = {"outputActive": False, "outputPaused": False, "outputDuration": 0}
        self.not_ready = False  # True: every request answers result=False, code=207
        self.auth_failures = 0
        self.identified_count = 0
        self.request_counts = {}
        self._base_ms = 0.0
        self._run_since = None  # monotonic time the duration clock last started, while it runs

    # ---- lifecycle ----

    @property
    def port(self):
        return self._port

    @property
    def running(self):
        return self._running

    def start(self):
        """Listen and serve. On a restart, binds the same port again, retrying for up to 2 s."""
        from websockets.sync.server import serve

        with self._lock:
            if self._running:
                raise RuntimeError("FakeObs is already running")
        sock = self._bind()
        self._port = sock.getsockname()[1]
        server = serve(
            self._handle,
            sock=sock,
            compression=None,
            open_timeout=2,
            close_timeout=1,
            logger=_QUIET_LOGGER,
        )
        thread = threading.Thread(target=_serve_quietly, args=(server,), name="fake-obs-server", daemon=True)
        with self._lock:
            self._server = server
            self._thread = thread
            self._running = True
        thread.start()
        return self

    def stop(self):
        """OBS exits: close every connection cleanly (a close frame), then stop listening."""
        self._shutdown(abort=False)

    def abort(self):
        """OBS crashes: drop every connection's socket without a close frame, then stop listening."""
        self._shutdown(abort=True)

    def _bind(self):
        deadline = time.monotonic() + BIND_RETRY_SECONDS
        while True:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if sys.platform != "win32":
                # Lets a restart bind past connections in TIME_WAIT. On Windows it would mean something
                # else (sharing a port another socket holds), and Windows doesn't need it for TIME_WAIT.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((self.host, self._port))
                sock.listen(16)
                return sock
            except OSError:
                sock.close()
                if self._port == 0 or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def _shutdown(self, abort):
        with self._lock:
            server, thread = self._server, self._thread
            self._server = None
            self._thread = None
            self._running = False
            conns = list(self._conns)
        if server is None:
            return
        # Stop listening first, so the client can't slip a reconnect in while the connections close.
        try:
            server.shutdown()
        except OSError:
            pass
        for conn in conns:
            if abort:
                _drop_socket(conn.socket)
            else:
                try:
                    conn.close()
                except Exception:
                    pass
        if thread is not None:
            thread.join(2)

    # ---- recording state ----

    def _fold_locked(self, now):
        if self._run_since is not None:
            self._base_ms += (now - self._run_since) * 1000.0
            self._run_since = now

    def set(self, active=None, paused=None, duration_ms=None):
        """Change the recording state. A new recording starts at 0 ms unless duration_ms is given."""
        with self._lock:
            now = time.monotonic()
            self._fold_locked(now)
            was_active = self.state["outputActive"]
            if active is not None:
                self.state["outputActive"] = bool(active)
            if paused is not None:
                self.state["outputPaused"] = bool(paused)
            if not self.state["outputActive"]:
                self.state["outputPaused"] = False
            if duration_ms is not None:
                self._base_ms = float(duration_ms)
            elif self.state["outputActive"] and not was_active:
                self._base_ms = 0.0
            runs = self.state["outputActive"] and not self.state["outputPaused"]
            self._run_since = now if runs else None
            self.state["outputDuration"] = int(self._base_ms)

    def duration_ms(self):
        """outputDuration right now: it advances only while recording and not paused."""
        with self._lock:
            self._fold_locked(time.monotonic())
            return self._base_ms

    def record_status(self):
        """The GetRecordStatus responseData, as OBS sends it."""
        with self._lock:
            return self._status_locked()

    def _status_locked(self):
        self._fold_locked(time.monotonic())
        self.state["outputDuration"] = int(self._base_ms)
        return {
            "outputActive": self.state["outputActive"],
            "outputPaused": self.state["outputPaused"],
            "outputTimecode": _timecode(self._base_ms),
            "outputDuration": int(self._base_ms),
            "outputBytes": 0,
        }

    def send_exit_started(self):
        """Send OBS's ExitStarted event to every identified client. Returns how many got it."""
        payload = json.dumps({"op": 5, "d": {"eventType": "ExitStarted", "eventIntent": 1, "eventData": {}}})
        with self._lock:
            conns = list(self._identified)
        sent = 0
        for conn in conns:
            try:
                conn.send(payload)
                sent += 1
            except Exception:
                pass
        return sent

    # ---- one connection ----

    def _handle(self, conn):
        with self._lock:
            accepted = self._running
            if accepted:
                self._conns.add(conn)
        if not accepted:
            return  # websockets closes it when the handler returns
        try:
            self._session(conn)
        except Exception:
            pass  # the client went away, or stop()/abort() closed the connection
        finally:
            with self._lock:
                self._conns.discard(conn)
                self._identified.discard(conn)

    def _next_message(self, conn):
        """The next JSON message, or None once the fake has stopped. Raises when the connection closes."""
        while self._running:
            try:
                text = conn.recv(timeout=RECV_SLICE)
            except TimeoutError:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if isinstance(message, dict):
                return message
        return None

    def _session(self, conn):
        hello = {"obsWebSocketVersion": "5.5.2", "rpcVersion": 1}
        salt = challenge = ""
        if self.password is not None:
            salt = base64.b64encode(os.urandom(24)).decode("ascii")
            challenge = base64.b64encode(os.urandom(24)).decode("ascii")
            hello["authentication"] = {"challenge": challenge, "salt": salt}
        conn.send(json.dumps({"op": 0, "d": hello}))

        identify = self._next_message(conn)
        if identify is None:
            return
        if identify.get("op") != 1:
            conn.close(4007, "Not identified.")
            return
        if self.password is not None:
            sent = (identify.get("d") or {}).get("authentication")
            if sent != obs_auth_string(self.password, salt, challenge):
                with self._lock:
                    self.auth_failures += 1
                conn.close(AUTH_FAILED_CODE, "Authentication failed.")
                return
        conn.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        with self._lock:
            self._identified.add(conn)
            self.identified_count += 1

        while True:
            message = self._next_message(conn)
            if message is None:
                return
            if message.get("op") == 6:
                conn.send(json.dumps(self._answer(message.get("d") or {})))

    def _answer(self, request):
        request_type = request.get("requestType")
        response = {"requestType": request_type, "requestId": request.get("requestId")}
        with self._lock:
            self.request_counts[request_type] = self.request_counts.get(request_type, 0) + 1
            not_ready = self.not_ready
            status = self._status_locked()
        if not_ready:
            response["requestStatus"] = {"result": False, "code": NOT_READY_CODE, "comment": "OBS is not ready."}
        else:
            response["requestStatus"] = {"result": True, "code": SUCCESS_CODE}
            response["responseData"] = status if request_type == "GetRecordStatus" else {}
        return {"op": 7, "d": response}
