"""Macrolens settings: defaults, an optional JSON settings file, and command-line flags, with validation."""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Optional

SETTINGS_FILE_NAME = "macrolens.settings.json"

DEFAULTS = {"host": "localhost", "port": 4455, "log_dir": "logs", "format": "tsv", "reconnect_seconds": 5}

ALLOWED_KEYS = tuple(DEFAULTS)
FORMATS = ("tsv", "json")

PASSWORD_MESSAGE = (
    "Don't put the OBS password in the settings file. Set the OBS_PASSWORD environment variable, "
    "or let Macrolens ask and save it to .obs_password."
)


class SettingsError(Exception):
    """A settings problem, with a message ready to show the user."""


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    log_dir: str  # always absolute
    format: str
    reconnect_seconds: float
    settings_path: Optional[str]  # the settings file actually read, or None


def read_settings_file(path) -> dict:
    """Read a settings file as a dict. Accepts a UTF-8 BOM, since Notepad writes one."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except UnicodeDecodeError as e:
        raise SettingsError(f"{path} is not UTF-8 text ({e.reason}). Save it as UTF-8 and run again.") from e
    except OSError as e:
        raise SettingsError(f"Could not read the settings file {path} ({e.strerror or e}).") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SettingsError(f"{path} is not valid JSON (line {e.lineno}, column {e.colno}): {e.msg}") from e
    if not isinstance(data, dict):
        raise SettingsError(
            f"{path} must hold one JSON object, like {{\"port\": 4455}}, not a {type(data).__name__}."
        )
    return data


def _check_keys(values, source):
    """Refuse a password key and any key Macrolens doesn't know."""
    for key in values:
        if key == "password":
            raise SettingsError(PASSWORD_MESSAGE)
        if key not in DEFAULTS:
            raise SettingsError(
                f"Unknown setting '{key}' in {source}. Allowed settings: {', '.join(ALLOWED_KEYS)}."
            )


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate(key, value, source):
    """Check one setting's value. Returns it in its stored form, or raises SettingsError."""
    if key == "host":
        if not isinstance(value, str) or not value.strip():
            raise SettingsError(f"host in {source} must be a non-empty name or address, like \"localhost\".")
        return value
    if key == "port":
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise SettingsError(
                f"port in {source} must be a whole number from 1 to 65535, written without quotes "
                f"(like 4455), not {value!r}."
            )
        return value
    if key == "format":
        if value not in FORMATS:
            raise SettingsError(f"format in {source} must be \"tsv\" or \"json\", not {value!r}.")
        return value
    if key == "reconnect_seconds":
        if not _is_number(value) or math.isnan(value) or not 1 <= value <= 300:
            raise SettingsError(
                f"reconnect_seconds in {source} must be a number of seconds from 1 to 300, "
                f"written without quotes (like 5), not {value!r}."
            )
        return float(value)
    if key == "log_dir":
        if not isinstance(value, str) or not value.strip():
            raise SettingsError(f"log_dir in {source} must be a non-empty folder path, like \"logs\".")
        return value
    raise SettingsError(f"Unknown setting '{key}' in {source}. Allowed settings: {', '.join(ALLOWED_KEYS)}.")


def _resolve_log_dir(value, base):
    """Expand ~ and environment variables, then make the folder absolute against base."""
    path = os.path.expandvars(os.path.expanduser(value))
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    return os.path.abspath(path)


def load_settings(cli: dict, script_dir: str, cwd: str) -> Settings:
    """Settings from the command line, else the settings file, else DEFAULTS, per key.

    cli holds only the flags the user actually gave. The settings file is cli["settings"] when given,
    else macrolens.settings.json beside the script when it exists, else none. log_dir is not created here.
    """
    cli = dict(cli)
    explicit = cli.pop("settings", None)
    _check_keys(cli, "the command line")

    if explicit is not None:
        settings_path = os.path.abspath(os.path.join(cwd, os.path.expanduser(explicit)))
        if not os.path.isfile(settings_path):
            raise SettingsError(f"Settings file not found: {settings_path}")
    else:
        default_path = os.path.abspath(os.path.join(script_dir, SETTINGS_FILE_NAME))
        settings_path = default_path if os.path.isfile(default_path) else None

    file_values = {}
    if settings_path is not None:
        file_values = read_settings_file(settings_path)
        _check_keys(file_values, settings_path)

    values = {}
    log_dir_base = None
    for key, default in DEFAULTS.items():
        if key in cli:
            value, source, base = cli[key], "the command line", cwd
        elif key in file_values:
            value, source, base = file_values[key], settings_path, os.path.dirname(settings_path)
        else:
            value, source, base = default, "the defaults", script_dir
        values[key] = _validate(key, value, source)
        if key == "log_dir":
            log_dir_base = base

    return Settings(
        host=values["host"],
        port=values["port"],
        log_dir=_resolve_log_dir(values["log_dir"], log_dir_base),
        format=values["format"],
        reconnect_seconds=values["reconnect_seconds"],
        settings_path=settings_path,
    )
