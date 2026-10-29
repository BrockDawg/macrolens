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

The log is plain tab-separated text, so any model or script reads it without extra tools.

## How it works

1. Macrolens connects to OBS over its built-in WebSocket server on `localhost`.
2. It checks about 30 times a second whether OBS is recording.
3. When recording starts, it opens a new log file in `logs/` and writes a `RECORD_START` line.
4. While recording, it logs every key press and release, mouse click, and scroll.
5. When recording stops, it writes `RECORD_STOP` and closes the file.
6. Between recordings, it logs nothing.

It sends nothing over the network except to OBS on your own computer.

## Privacy: read this first

**Macrolens is a keylogger.** It records every key you press in every window while OBS records.
That includes passwords, private messages and anything else you type.

- Pause the OBS recording before you type a password or anything private.
- Logs stay on your computer, in the `logs/` folder. The folder is in `.gitignore`.
- Delete logs and videos before you share a folder.
- Run it only on a computer you own, and only record yourself.
  Recording other people without their consent is illegal in many places.

## Setup

You need OBS 28 or later (it includes the WebSocket server) and Python 3.9 or later.

1. In OBS, open **Tools, WebSocket Server Settings**, tick **Enable WebSocket server** and keep port `4455`.
2. Open OBS, then start Macrolens:

   ```
   python macrolens.py
   ```

3. The first time, Macrolens does two things for you:
   - If the packages in `requirements.txt` are missing, it asks whether to install them with pip, and installs them if you say yes.
   - If it has no OBS password stored, it asks for one (press Enter if OBS has none) and offers to save it.
     The password is saved in a `.obs_password` file beside `macrolens.py`. The file is in `.gitignore`. Delete it to be asked again.
     To skip the question, set the `OBS_PASSWORD` environment variable instead; it wins over the saved file.
4. Press record in OBS, do your task, and stop the recording.
5. Press `Ctrl+C` in the Macrolens window to quit.

If OBS is not open when you start Macrolens, it stops with a connection error.

## The log format

One event per line, fields separated by tabs.
Logs are named `YYYY-MM-DD_HH-MM-SS_task_log.tsv` and are UTF-8.

| Field | Meaning |
|---|---|
| 1 | Wall-clock time, `YYYY-MM-DD HH:MM:SS.mmm` |
| 2 | Event: `RECORD_START`, `RECORD_STOP`, `key_down`, `key_up`, `mouse_down`, `mouse_up`, `scroll` |
| 3 | Title of the focused window (Windows only, empty on other systems) |
| 4 | Key name, or the mouse button, or the scroll amount `dx,dy` |
| 5 | Mouse and scroll events only: position `x,y` |

Example:

```
2026-10-29 14:02:11.482	RECORD_START
2026-10-29 14:02:13.910	mouse_down	Invoices - Excel	left	642,318
2026-10-29 14:02:13.988	mouse_up	Invoices - Excel	left	642,318
2026-10-29 14:02:15.204	key_down	Invoices - Excel	a
2026-10-29 14:02:15.260	key_up	Invoices - Excel	a
```

To match the log to the video, take the time of the `RECORD_START` line as second 0 of the video.
The match is accurate to about a tenth of a second.

## Using it with an AI

Attach the `.tsv` log (and a few screenshots or the video) and ask something like:

> This log is a recording of me doing a task. The window titles show which app I was in.
> Write a Python script that repeats the task, and list any step you are unsure about.

Check what the AI writes before you run it.
Run a new automation on test data first.

## Limits

- Mouse movement is not logged, only clicks and scrolls.
- The focused window title works on Windows only.
- Keys are logged as the system reports them, so shortcuts show as separate key presses.
- A game or app running as administrator may hide its input from Macrolens unless you run Macrolens as administrator too.

## Credit and licence

Macrolens is a modified fork of [PINTO0309/obs-key-logger](https://github.com/PINTO0309/obs-key-logger), Copyright its authors, released under the Apache License 2.0.
This fork is released under the same licence. See [LICENSE](LICENSE).

Changes from the original:

- Records only while OBS is recording. The original kept keys in memory at all times and wrote them out when recording began.
- Logs key releases, mouse clicks, scrolls and the focused window title.
- Esc no longer quits the program.
- Logs go to a `logs/` folder as tab-separated text.
- Renamed to `macrolens.py`. The MP4 player, C# logger and build files were removed.

Macrolens is not made by or endorsed by the OBS Project or the original author.
