"""Tests for macrolens_settings: defaults, the settings file, CLI precedence, validation and log_dir resolution."""
from __future__ import annotations

import json
import os

import pytest

import macrolens_settings
from macrolens_settings import DEFAULTS, SETTINGS_FILE_NAME, Settings, SettingsError, load_settings


def same_path(a, b):
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


@pytest.fixture
def dirs(tmp_path):
    script_dir = tmp_path / "app"
    cwd = tmp_path / "work"
    conf = tmp_path / "conf"
    for d in (script_dir, cwd, conf):
        d.mkdir()
    return script_dir, cwd, conf


def write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def load(cli, script_dir, cwd):
    return load_settings(cli, str(script_dir), str(cwd))


# --- cold start and the file ---------------------------------------------------------------------


def test_cold_start_equals_defaults(dirs):
    script_dir, cwd, _ = dirs
    s = load({}, script_dir, cwd)
    assert isinstance(s, Settings)
    assert s.host == DEFAULTS["host"]
    assert s.port == DEFAULTS["port"]
    assert s.format == DEFAULTS["format"]
    assert s.reconnect_seconds == float(DEFAULTS["reconnect_seconds"])
    assert isinstance(s.reconnect_seconds, float)
    assert os.path.isabs(s.log_dir)
    assert same_path(s.log_dir, script_dir / "logs")
    assert s.settings_path is None


def test_default_file_beside_script_is_read(dirs):
    script_dir, cwd, _ = dirs
    path = write_json(
        script_dir / SETTINGS_FILE_NAME,
        {"host": "obs-pc", "port": 4460, "format": "json", "reconnect_seconds": 10},
    )
    s = load({}, script_dir, cwd)
    assert (s.host, s.port, s.format, s.reconnect_seconds) == ("obs-pc", 4460, "json", 10.0)
    assert same_path(s.settings_path, path)


# key -> (value in the file, value on the command line)
GRID = {
    "host": ("file-host", "cli-host"),
    "port": (1111, 2222),
    "format": ("json", "tsv"),
    "reconnect_seconds": (7, 2.5),
}


@pytest.mark.parametrize("key", sorted(GRID))
@pytest.mark.parametrize("where", ["file", "cli", "both"])
def test_cli_beats_file_per_key(dirs, key, where):
    script_dir, cwd, _ = dirs
    file_value, cli_value = GRID[key]
    if where in ("file", "both"):
        write_json(script_dir / SETTINGS_FILE_NAME, {key: file_value})
    cli = {key: cli_value} if where in ("cli", "both") else {}
    s = load(cli, script_dir, cwd)
    expected = file_value if where == "file" else cli_value
    assert getattr(s, key) == expected
    for other in GRID:
        if other != key:
            assert getattr(s, other) == DEFAULTS[other]


def test_cli_format_json_without_file(dirs):
    script_dir, cwd, _ = dirs
    assert load({"format": "json"}, script_dir, cwd).format == "json"


def test_bom_file_parses(dirs):
    script_dir, cwd, _ = dirs
    (script_dir / SETTINGS_FILE_NAME).write_text(json.dumps({"port": 4470}), encoding="utf-8-sig")
    assert (script_dir / SETTINGS_FILE_NAME).read_bytes().startswith(b"\xef\xbb\xbf")
    assert load({}, script_dir, cwd).port == 4470


def test_bad_json_names_line_and_column(dirs):
    script_dir, cwd, _ = dirs
    text = '{\n  "port": 4455\n  "host": "x"\n}\n'
    path = script_dir / SETTINGS_FILE_NAME
    path.write_text(text, encoding="utf-8")
    with pytest.raises(json.JSONDecodeError) as real:
        json.loads(text)
    with pytest.raises(SettingsError) as err:
        load({}, script_dir, cwd)
    message = str(err.value)
    assert "is not valid JSON" in message
    assert f"(line {real.value.lineno}, column {real.value.colno})" in message
    assert real.value.lineno == 3
    assert str(path) in message


def test_read_settings_file_rejects_a_non_object(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(SettingsError, match="one JSON object"):
        macrolens_settings.read_settings_file(str(path))


# --- validation ----------------------------------------------------------------------------------


def test_unknown_key_is_named_with_the_allowed_keys(dirs):
    script_dir, cwd, _ = dirs
    write_json(script_dir / SETTINGS_FILE_NAME, {"colour": "red"})
    with pytest.raises(SettingsError) as err:
        load({}, script_dir, cwd)
    message = str(err.value)
    assert "Unknown setting 'colour'" in message
    assert "host, port, log_dir, format, reconnect_seconds" in message


def test_password_key_has_its_own_message(dirs):
    script_dir, cwd, _ = dirs
    write_json(script_dir / SETTINGS_FILE_NAME, {"password": "hunter2"})
    with pytest.raises(SettingsError) as err:
        load({}, script_dir, cwd)
    message = str(err.value)
    assert message == macrolens_settings.PASSWORD_MESSAGE
    assert "OBS_PASSWORD" in message
    assert ".obs_password" in message
    assert "hunter2" not in message


@pytest.mark.parametrize("bad", [True, "4455", 0, 65536, 4455.0, None])
def test_bad_ports_raise(dirs, bad):
    script_dir, cwd, _ = dirs
    write_json(script_dir / SETTINGS_FILE_NAME, {"port": bad})
    with pytest.raises(SettingsError) as err:
        load({}, script_dir, cwd)
    message = str(err.value)
    assert message.startswith("port in ")
    assert "1 to 65535" in message
    assert "without quotes" in message
    assert repr(bad) in message


def test_bad_port_from_cli_names_the_command_line(dirs):
    script_dir, cwd, _ = dirs
    with pytest.raises(SettingsError, match="port in the command line"):
        load({"port": 0}, script_dir, cwd)


@pytest.mark.parametrize("good", [1, 65535])
def test_port_edges_are_accepted(dirs, good):
    script_dir, cwd, _ = dirs
    assert load({"port": good}, script_dir, cwd).port == good


@pytest.mark.parametrize("bad", ["csv", "TSV", "", 1])
def test_bad_format_raises(dirs, bad):
    script_dir, cwd, _ = dirs
    write_json(script_dir / SETTINGS_FILE_NAME, {"format": bad})
    with pytest.raises(SettingsError, match='format in .* must be "tsv" or "json"'):
        load({}, script_dir, cwd)


@pytest.mark.parametrize("bad", [True, "5", 0, 0.5, 301, None])
def test_bad_reconnect_seconds_raise(dirs, bad):
    script_dir, cwd, _ = dirs
    write_json(script_dir / SETTINGS_FILE_NAME, {"reconnect_seconds": bad})
    with pytest.raises(SettingsError, match="reconnect_seconds in .* from 1 to 300"):
        load({}, script_dir, cwd)


@pytest.mark.parametrize("good", [1, 300, 2.5])
def test_reconnect_seconds_edges_are_accepted(dirs, good):
    script_dir, cwd, _ = dirs
    assert load({"reconnect_seconds": good}, script_dir, cwd).reconnect_seconds == float(good)


@pytest.mark.parametrize("bad", ["", "   ", 5, None])
def test_bad_host_raises(dirs, bad):
    script_dir, cwd, _ = dirs
    write_json(script_dir / SETTINGS_FILE_NAME, {"host": bad})
    with pytest.raises(SettingsError, match="host in .* non-empty"):
        load({}, script_dir, cwd)


# --- which file is read --------------------------------------------------------------------------


def test_missing_explicit_settings_file_errors(dirs):
    script_dir, cwd, _ = dirs
    with pytest.raises(SettingsError) as err:
        load({"settings": "nope.json"}, script_dir, cwd)
    message = str(err.value)
    assert message.startswith("Settings file not found: ")
    assert same_path(message[len("Settings file not found: "):], cwd / "nope.json")


def test_missing_default_settings_file_is_fine(dirs):
    script_dir, cwd, _ = dirs
    assert not (script_dir / SETTINGS_FILE_NAME).exists()
    assert load({}, script_dir, cwd).settings_path is None


def test_explicit_settings_file_wins_over_the_default_file(dirs):
    script_dir, cwd, _ = dirs
    write_json(script_dir / SETTINGS_FILE_NAME, {"port": 1111})
    explicit = write_json(cwd / "mine.json", {"port": 2222})
    s = load({"settings": "mine.json"}, script_dir, cwd)
    assert s.port == 2222
    assert same_path(s.settings_path, explicit)


# --- log_dir resolution --------------------------------------------------------------------------


def test_log_dir_from_cli_resolves_against_cwd(dirs):
    script_dir, cwd, conf = dirs
    write_json(conf / "s.json", {"log_dir": "from-file"})
    s = load({"log_dir": "from-cli", "settings": str(conf / "s.json")}, script_dir, cwd)
    assert same_path(s.log_dir, cwd / "from-cli")


def test_log_dir_from_file_resolves_against_the_files_folder(dirs):
    script_dir, cwd, conf = dirs
    write_json(conf / "s.json", {"log_dir": "from-file"})
    s = load({"settings": str(conf / "s.json")}, script_dir, cwd)
    assert same_path(s.log_dir, conf / "from-file")


def test_log_dir_default_resolves_against_script_dir_even_with_a_file(dirs):
    script_dir, cwd, conf = dirs
    write_json(conf / "s.json", {"port": 4455})
    s = load({"settings": str(conf / "s.json")}, script_dir, cwd)
    assert same_path(s.log_dir, script_dir / "logs")


def test_absolute_log_dir_is_kept(dirs, tmp_path):
    script_dir, cwd, _ = dirs
    target = tmp_path / "elsewhere"
    write_json(script_dir / SETTINGS_FILE_NAME, {"log_dir": str(target)})
    assert same_path(load({}, script_dir, cwd).log_dir, target)


@pytest.mark.parametrize("source", ["cli", "file"])
def test_log_dir_expands_tilde(dirs, tmp_path, monkeypatch, source):
    script_dir, cwd, _ = dirs
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    if source == "cli":
        s = load({"log_dir": "~/macrolens-logs"}, script_dir, cwd)
    else:
        write_json(script_dir / SETTINGS_FILE_NAME, {"log_dir": "~/macrolens-logs"})
        s = load({}, script_dir, cwd)
    assert same_path(s.log_dir, home / "macrolens-logs")


def test_log_dir_expands_environment_variables(dirs, tmp_path, monkeypatch):
    script_dir, cwd, _ = dirs
    monkeypatch.setenv("MACROLENS_TEST_LOGS", str(tmp_path / "env-logs"))
    s = load({"log_dir": "$MACROLENS_TEST_LOGS/sub"}, script_dir, cwd)
    assert same_path(s.log_dir, tmp_path / "env-logs" / "sub")


def test_log_dir_is_not_created(dirs):
    script_dir, cwd, _ = dirs
    s = load({"log_dir": "not-yet"}, script_dir, cwd)
    assert not os.path.exists(s.log_dir)
    assert not (script_dir / "logs").exists()
