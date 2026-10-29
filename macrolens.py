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
import ctypes
import getpass
import importlib
import importlib.util
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REQUIREMENTS_FILE = os.path.join(HERE, "requirements.txt")
PASSWORD_FILE = os.path.join(HERE, ".obs_password")  # git-ignored; an empty file means "OBS has no password"

# import name -> pip package, for the check below
REQUIRED_MODULES = {"pynput": "pynput", "obswebsocket": "obs-websocket-py"}


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


ensure_requirements()

from obswebsocket import obsws, requests  # noqa: E402
from pynput import keyboard, mouse  # noqa: E402

# OBS WebSocket settings
OBS_HOST = "localhost"
OBS_PORT = 4455

POLL_INTERVAL = 1 / 30

LOG_DIR = os.path.join(HERE, "logs")


def get_obs_password():
    """The OBS password: the OBS_PASSWORD environment variable, else the stored one, else ask once and offer to store it."""
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
    ws = obsws(OBS_HOST, OBS_PORT, get_obs_password())
    try:
        ws.connect()
    except Exception as err:
        sys.exit(
            f"Could not reach OBS at {OBS_HOST}:{OBS_PORT} ({err}).\n"
            "Open OBS and turn on Tools > WebSocket Server Settings > Enable WebSocket server.\n"
            f"If the password is wrong, delete {PASSWORD_FILE} (or fix the OBS_PASSWORD environment variable) and run again."
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
