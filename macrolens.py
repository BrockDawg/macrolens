# Original work: https://github.com/PINTO0309/obs-key-logger (Apache License 2.0, see LICENSE).
# MODIFIED by Brock (BrockDawg), 2026-10-29. Changes from the original:
#   - Keys, mouse clicks and scrolls are recorded only while OBS is recording
#     (the original buffered keys at all times and wrote them out when recording began).
#   - Key releases, mouse clicks, scrolls and the active window title are logged.
#   - Esc is logged like any other key. Stop the program with Ctrl+C or by closing its window.
#   - Logs go to a logs/ folder beside this file, one tab-separated line per event.
#   - A RECORD_START line and a RECORD_STOP line carry wall-clock times to line the log up with the video.
#   - Renamed from obs-key-logger.py to macrolens.py. The MP4 player, C# logger and build files were removed.
import ctypes
import os
import sys
import threading
import time
from datetime import datetime

from obswebsocket import obsws, requests
from pynput import keyboard, mouse

# OBS WebSocket settings
OBS_HOST = "localhost"
OBS_PORT = 4455
OBS_PASSWORD = ""  # set this if you enabled a password in OBS

POLL_INTERVAL = 1 / 30

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

lock = threading.Lock()
recording = False
log_file = None


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def active_window_title():
    """Title of the focused window. Windows only, empty elsewhere."""
    if sys.platform != "win32":
        return ""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value.replace("\t", " ").replace("\n", " ")
    except Exception:
        return ""


def record(kind, detail):
    """Write one event. Does nothing unless OBS is recording."""
    with lock:
        if not recording or log_file is None:
            return
        log_file.write(f"{now()}\t{kind}\t{active_window_title()}\t{detail}\n")
        log_file.flush()


def key_name(key):
    char = getattr(key, "char", None)
    return char if char is not None else str(key)


def on_press(key):
    record("key_down", key_name(key))


def on_release(key):
    record("key_up", key_name(key))


def on_click(x, y, button, pressed):
    record("mouse_down" if pressed else "mouse_up", f"{button.name}\t{x},{y}")


def on_scroll(x, y, dx, dy):
    record("scroll", f"{dx},{dy}\t{x},{y}")


def start_log():
    global log_file, recording
    os.makedirs(LOG_DIR, exist_ok=True)
    name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "_task_log.tsv"
    path = os.path.join(LOG_DIR, name)
    with lock:
        log_file = open(path, "a", encoding="utf-8")
        log_file.write(f"{now()}\tRECORD_START\t\t\n")
        recording = True
    print(f"Recording started. Log: {path}")


def stop_log():
    global log_file, recording
    with lock:
        recording = False
        if log_file is not None:
            log_file.write(f"{now()}\tRECORD_STOP\t\t\n")
            log_file.close()
            log_file = None
    print("Recording stopped.")


def main_loop():
    ws = obsws(OBS_HOST, OBS_PORT, OBS_PASSWORD)
    try:
        ws.connect()
    except Exception as err:
        sys.exit(
            f"Could not reach OBS at {OBS_HOST}:{OBS_PORT} ({err}).\n"
            "Open OBS and turn on Tools > WebSocket Server Settings > Enable WebSocket server."
        )
    print("Connected to OBS WebSocket. Press Ctrl+C to quit.")
    try:
        while True:
            status = ws.call(requests.GetRecordStatus())
            is_recording = status.datain.get("outputActive", False)
            if is_recording and not recording:
                start_log()
            elif not is_recording and recording:
                stop_log()
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("Stopped by user")
    finally:
        if recording:
            stop_log()
        ws.disconnect()


if __name__ == "__main__":
    kb = keyboard.Listener(on_press=on_press, on_release=on_release)
    ms = mouse.Listener(on_click=on_click, on_scroll=on_scroll)
    kb.start()
    ms.start()
    try:
        main_loop()
    finally:
        kb.stop()
        ms.stop()
