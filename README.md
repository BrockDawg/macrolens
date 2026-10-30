<p align="center">
  <img src="assets/logo.svg" alt="Macrolens logo" width="160">
</p>

<h1 align="center">Macrolens</h1>

<p align="center">
  Record a task once, with the screen and every key and click.<br>
  Hand both to an AI later and turn the task into an automation.
</p>

---

## What it is

Macrolens is a small Python tool that runs beside [OBS Studio](https://obsproject.com/).
While OBS records your screen, Macrolens logs what you do on the keyboard and mouse.
It writes one timestamped line for each event, and it records the title of the focused window with each line.

You end up with two files that line up in time:

| File | What it holds |
|---|---|
| The OBS video | What the screen looked like, and why you pressed each key |
| The Macrolens log | The exact keys, clicks, scrolls and window titles, with times |

## Why it exists

Most automation starts with someone describing a task in words, and the words leave steps out.
Macrolens captures the task as you really do it, so nothing is left to memory.

It is built to work alongside AI.
Give a model the log and the video, and ask it to write the automation:

- A script that repeats the task (RPA, robotic process automation).
- A step-by-step runbook for a teammate.
- A macro for a tool such as AutoHotkey.
- A test that checks the task still works.

The log is plain text, tab-separated by default or JSON Lines if you ask for it, so any model or script reads it without extra tools.

## How it works

1. Macrolens connects to OBS over its built-in WebSocket server, on `localhost` port `4455` unless you set another host or port.
2. If OBS is not open yet, Macrolens says once that it is waiting, and keeps trying (every 5 seconds by default) until OBS answers.
3. It checks about 30 times a second whether OBS is recording, and whether the recording is paused.
4. When a recording starts, it opens a new log file in `logs/` (or the folder you set) and writes a header row and a `RECORD_START` line.
   Each OBS recording gets its own log file, so there is one log per video.
5. While recording, it logs every key press and release, mouse click, and scroll.
6. When you pause the recording in OBS, it writes `RECORD_PAUSE` and logs nothing until you resume, when it writes `RECORD_RESUME`.
7. When the recording stops, it writes `RECORD_STOP` with the reason it stopped, and closes the file.
8. Between recordings, it logs nothing.
9. If OBS closes or crashes, Macrolens ends the open log with a `RECORD_STOP` line, waits for OBS to come back, and reconnects on its own.
   A recording after that goes into a new log file.

It sends nothing over the network except to OBS, which is on your own computer unless you set another host.

## Privacy: read this first

**Macrolens is a keylogger.** It records every key you press in every window while OBS records.
That includes passwords, private messages and anything else you type.

- Pause the OBS recording before you type a password or anything private.
  Pausing really stops the logging: nothing is written between `RECORD_PAUSE` and `RECORD_RESUME`.
  Before 2.0 it did not, and keys typed while OBS was paused were still logged.
- Logs stay on your computer, in the `logs/` folder unless you set another one.
  The `logs/` folder is in `.gitignore`.
- On Mac and Linux, the log folder and each log file can be read only by your own user.
  On Windows, your user profile's normal permissions apply.
- If you set a log folder inside a git repo, or inside a synced folder such as OneDrive, Dropbox, Google Drive or iCloud, Macrolens prints a warning at start, because keystroke logs there could leave this computer.
  It is a warning only; Macrolens still runs.
- Delete logs and videos before you share a folder.
- Run it only on a computer you own, and only record yourself.
  Recording other people without their consent is illegal in many places.

## Setup

You need OBS 28 or later (it includes the WebSocket server) and Python 3.10 or later.

1. In OBS, open **Tools, WebSocket Server Settings**, tick **Enable WebSocket server** and keep port `4455`.
2. Start Macrolens:

   ```
   python macrolens.py
   ```

   You can start it before or after OBS.
   If OBS is not open yet, Macrolens waits for it and connects when it is.

3. The first time, Macrolens does two things for you:
   - If the packages in `requirements.txt` are missing, it asks whether to install them with pip, and installs them if you say yes.
   - If it has no OBS password stored, it asks for one (press Enter if OBS has none) and offers to save it.
     The password is saved in a `.obs_password` file beside `macrolens.py`. The file is in `.gitignore`. Delete it to be asked again.
     To skip the question, set the `OBS_PASSWORD` environment variable instead; it wins over the saved file.
4. Press record in OBS, do your task, and stop the recording.
5. Press `Ctrl+C` in the Macrolens window to quit.
   On Windows, closing the window also ends the open log cleanly.

To see every option, run:

```
python macrolens.py --help
```

`python macrolens.py --version` prints the version.
Both work before the packages are installed.

If OBS refuses the password twice in a row, Macrolens stops and tells you to delete `.obs_password` or fix `OBS_PASSWORD`.
Any other problem reaching OBS (OBS still starting, closing, or not yet open) just sends it back to waiting.

### Settings

Macrolens works with no settings file.
To change its defaults, copy `macrolens.settings.example.json` to `macrolens.settings.json` beside `macrolens.py` and edit it.
`macrolens.settings.json` is in `.gitignore`.
To use a settings file somewhere else, pass `--settings PATH`.
A file given with `--settings` must exist; the default file is optional.

The file is one JSON object with any of these keys:

| Key | Default | Flag | Meaning |
|---|---|---|---|
| `host` | `"localhost"` | `--host` | The computer OBS runs on. |
| `port` | `4455` | `--port` | The OBS WebSocket server port, a whole number from 1 to 65535, written without quotes. |
| `log_dir` | `"logs"` | `--log-dir` | The folder logs are written to. |
| `format` | `"tsv"` | `--format` | `"tsv"` for tab-separated text, or `"json"` for JSON Lines. |
| `reconnect_seconds` | `5` | `--reconnect-seconds` | Seconds between tries to reach OBS, a number from 1 to 300. |

A flag on the command line wins over the file, and the file wins over the defaults, key by key.

`log_dir` may start with `~` or contain environment variables such as `%USERPROFILE%` or `$HOME`.
A relative `log_dir` is read against the folder it came from: the settings file's folder for a value in the file, the current folder for `--log-dir`, and the folder of `macrolens.py` for the default.

Macrolens refuses a settings file with a key it does not know, a value of the wrong type (such as a port in quotes), or JSON it cannot read, and the message names the key, or the line and column, to fix.
Files saved by Notepad with a byte-order mark are fine.

**The OBS password never goes in the settings file.**
A `password` key is refused.
Use the `OBS_PASSWORD` environment variable, or let Macrolens ask and save it to `.obs_password`.

## The log format

This is log format version 2, written by Macrolens 2.0.
Logs are UTF-8, one event per line, and are named `YYYY-MM-DD_HH-MM-SS_task_log.tsv` (or `.jsonl` for JSON Lines).
If two recordings start in the same second, the second log gets `_2` before the extension, then `_3`, and so on.

### TSV (the default)

Line 1 is always the header row: `time`, `video_time`, `event`, `window`, `detail`, `position`, separated by tabs.
Every line after it has exactly six tab-separated fields; a field with nothing in it is left empty.

| Column | Meaning |
|---|---|
| `time` | Local wall-clock time, `YYYY-MM-DD HH:MM:SS.mmm`. |
| `video_time` | Seconds into the OBS video, with pauses taken out, to three decimals. |
| `event` | What happened (see the event kinds below). |
| `window` | Title of the focused window. Windows only; empty on other systems and on `RECORD_*` and `NOTE` lines. |
| `detail` | Key name, mouse button, or scroll amount `dx,dy`. On `RECORD_START`, the recording facts; on `RECORD_STOP`, the reason; on `NOTE`, a short message. |
| `position` | `x,y` for mouse and scroll events, in screen pixels; empty otherwise. |

Event kinds:

| Event | Meaning |
|---|---|
| `RECORD_START` | OBS started recording (or was already recording when Macrolens connected). Always the first event in a log. |
| `RECORD_PAUSE` | You paused the recording in OBS. Nothing is logged until `RECORD_RESUME`. |
| `RECORD_RESUME` | You resumed the recording. |
| `RECORD_STOP` | The log ends here. `detail` gives `reason=<reason>`. |
| `NOTE` | Something Macrolens wants you to know, such as `keyboard listener restarted`. |
| `key_down`, `key_up` | A key was pressed or released. |
| `mouse_down`, `mouse_up` | A mouse button was pressed or released. |
| `scroll` | The mouse wheel moved. |

Key names are the character typed (`a`, `A`, `1`), or a name like `Key.shift`, `Key.enter` or `Key.ctrl_l` for keys with no character.
A control character, which you get by holding Ctrl with a letter, is written as `^` and the letter, so Ctrl+S shows as `^S`.
Tabs and line breaks in a window title become spaces, so a line can never break.

Stop reasons, in the `detail` of `RECORD_STOP`:

| Reason | Meaning |
|---|---|
| `obs_stopped` | You stopped the recording in OBS. |
| `obs_exited` | You closed OBS while it was recording (Macrolens saw OBS start to shut down). |
| `obs_disconnected` | OBS crashed, was killed, or stopped answering. `last_confirmed=<time>` says when OBS last confirmed it was recording. |
| `user_quit` | You pressed `Ctrl+C` in the Macrolens window. |
| `window_closed` | You closed the Macrolens window, or Windows logged you off or shut down. |
| `terminated` | Macrolens was told to stop by the system (SIGTERM or SIGHUP, on Mac or Linux). |
| `error` | Macrolens stopped on an unexpected error. |
| `unclean_exit` | Macrolens was killed or the computer lost power before it could write a stop line. The next time Macrolens starts, it adds this line to the unfinished log, using the time of the last line that was written, and prints the file's name. |

### `video_time`

`video_time` is the number of seconds into the OBS video at which the event happened.
It comes from OBS's own recording clock, which already leaves out paused time.
So it is right even when you started Macrolens after the recording began, and it does not move when the computer's clock is adjusted.
While the recording is paused it stays at the moment of the pause, so `RECORD_PAUSE` and `RECORD_RESUME` show the same value.
To find an event in the video, seek to its `video_time`.

### The `RECORD_START` line

The `detail` of `RECORD_START` holds `key=value` pairs separated by spaces, in this order:

| Key | Meaning |
|---|---|
| `macrolens` | The Macrolens version, such as `2.0.0`. |
| `log_format` | The log format version, `2`. |
| `utc_offset` | Your time zone's offset from UTC, such as `-05:00`, so `time` can be read as an exact moment. |
| `obs` | The OBS host and port Macrolens was connected to. |
| `obs_duration_ms` | How far into the recording OBS was when the log started, in milliseconds (empty if OBS did not say). |
| `screen` | Windows only: the size of the whole desktop across all monitors, `<width>x<height>`, in physical pixels. |
| `screen_origin` | Windows only: the top-left corner of that desktop, `x,y`. It can be negative when a monitor sits left of or above the main one. |
| `dpi_scale` | Windows only: the display scaling, such as `1.25` for 125%. |

`screen`, `screen_origin` and `dpi_scale` are what an AI needs to map a click's `position` onto the video.
Positions use the same physical pixels and the same coordinates as `screen_origin`, where the main monitor's top-left corner is `0,0`.
Each of the three is left out if Windows does not report it.

Example:

```
time	video_time	event	window	detail	position
2026-10-29 14:02:11.482	1.520	RECORD_START		macrolens=2.0.0 log_format=2 utc_offset=-05:00 obs=localhost:4455 obs_duration_ms=1520 screen=2560x1440 screen_origin=0,0 dpi_scale=1.25	
2026-10-29 14:02:13.910	3.948	mouse_down	Invoices - Excel	left	642,318
2026-10-29 14:02:13.988	4.026	mouse_up	Invoices - Excel	left	642,318
2026-10-29 14:02:15.204	5.242	key_down	Invoices - Excel	a	
2026-10-29 14:02:15.260	5.298	key_up	Invoices - Excel	a	
2026-10-29 14:02:16.001	6.039	key_down	Invoices - Excel	Key.ctrl_l	
2026-10-29 14:02:16.120	6.158	key_down	Invoices - Excel	^S	
2026-10-29 14:02:18.400	8.438	RECORD_PAUSE			
2026-10-29 14:02:31.050	8.438	RECORD_RESUME			
2026-10-29 14:02:40.700	18.088	RECORD_STOP		reason=obs_stopped	
```

### JSON Lines (`--format json`)

With `--format json` (or `"format": "json"` in the settings file), the log is a `.jsonl` file with one JSON object per line.
Each object has the same six keys as the TSV columns, with the same values.
The one difference is that `video_time` is a number, not text.
There is no header row; line 1 is always `RECORD_START`, which also carries a `meta` object holding the same facts as its `detail`, with `log_format` as a number.
Text is written as is, so window titles in any language stay readable.

The same recording as above, first, second and last lines:

```
{"time": "2026-10-29 14:02:11.482", "video_time": 1.52, "event": "RECORD_START", "window": "", "detail": "macrolens=2.0.0 log_format=2 utc_offset=-05:00 obs=localhost:4455 obs_duration_ms=1520 screen=2560x1440 screen_origin=0,0 dpi_scale=1.25", "position": "", "meta": {"macrolens": "2.0.0", "log_format": 2, "utc_offset": "-05:00", "obs": "localhost:4455", "obs_duration_ms": "1520", "screen": "2560x1440", "screen_origin": "0,0", "dpi_scale": "1.25"}}
{"time": "2026-10-29 14:02:13.910", "video_time": 3.948, "event": "mouse_down", "window": "Invoices - Excel", "detail": "left", "position": "642,318"}
{"time": "2026-10-29 14:02:40.700", "video_time": 18.088, "event": "RECORD_STOP", "window": "", "detail": "reason=obs_stopped", "position": ""}
```

### Older logs

Logs written before 2.0 (format version 1) have no header row, no `video_time` column and no stop reason.
Their columns are `time`, `event`, `window`, `detail` and `position`, and second 0 of the video is the time of their `RECORD_START` line.
Macrolens leaves them as they are.

## Using it with an AI

Attach the log (and a few screenshots or the video).
The header row and the `RECORD_START` line explain the file, so you don't need to describe the format to the model.
If your tool prefers JSON, record with `--format json`.
Then ask something like:

> This log is a recording of me doing a task. The window titles show which app I was in.
> `video_time` is the second in the video where each event happened.
> Write a Python script that repeats the task, and list any step you are unsure about.

Check what the AI writes before you run it.
Run a new automation on test data first.

## Limits

- Mouse movement is not logged, only clicks and scrolls.
- The focused window title, and the `screen`, `screen_origin` and `dpi_scale` facts, work on Windows only.
- Windows is the system Macrolens is built and tested for.
  The automated tests also run on Linux; nothing is tested on macOS.
  On a Mac the core logging should work, but there are no window titles, and a closed Terminal window ends the log as `terminated`.
- Keys are logged as the system reports them, so shortcuts show as separate key presses.
- A game or app running as administrator may hide its input from Macrolens unless you run Macrolens as administrator too.
- When OBS crashes or is killed, Macrolens can take up to 2 seconds to notice, and input in that time is still logged.
  The `last_confirmed` time on the `RECORD_STOP` line says when OBS last confirmed it was recording, so input after it can be ignored.
- While OBS is busy (closing, or switching scene collection), it can answer "not ready" for a moment.
  Macrolens keeps the log open and keeps logging for up to 2 seconds of that, then treats OBS as gone (`obs_disconnected`).
- If Macrolens itself is killed or the computer loses power, the log has no stop line until the next time Macrolens starts with the same log folder, which adds `reason=unclean_exit`.
  Lines are saved to disk about once a second, so a power cut can lose about the last second.
- If you "Kick" Macrolens from OBS's WebSocket Server Settings, it reconnects on its own.
  To stop it, close Macrolens.
- Only one Macrolens can run per log folder.
  A second copy says it is already running and exits.

## Credit and licence

Macrolens is a modified fork of [PINTO0309/obs-key-logger](https://github.com/PINTO0309/obs-key-logger), Copyright its authors, released under the Apache License 2.0.
This fork is released under the same licence. See [LICENSE](LICENSE).

Changes from the original:

- Records only while OBS is recording. The original kept keys in memory at all times and wrote them out when recording began.
- Logs key releases, mouse clicks, scrolls and the focused window title.
- Esc no longer quits the program.
- Logs go to a `logs/` folder as tab-separated text.
- Renamed to `macrolens.py`. The MP4 player, C# logger and build files were removed.
- Offers to install the requirements when they are missing, and asks for the OBS password when none is stored.
- 2.0: honours OBS pause, so nothing is logged while the recording is paused.
- 2.0: waits for OBS and reconnects when OBS restarts, with a new log file for each recording.
- 2.0: every log ends with a `RECORD_STOP` line that gives the reason, including logs left unfinished by a crash, which are sealed at the next start.
- 2.0: `--help`, `--version` and a settings file (`macrolens.settings.json`).
- 2.0: log format v2, with a header row, a `video_time` column, `RECORD_START` metadata and optional JSON Lines output.
- 2.0: one Macrolens per log folder, private log files on Mac and Linux, and a warning when the log folder is in a git repo or a synced folder.

Macrolens is not made by or endorsed by the OBS Project or the original author.
