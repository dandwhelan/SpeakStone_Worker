"""Tests never reach GitHub, never open a tray icon, never swap a program, never start the real service, and never
touch a real install.

Why the last one: on 2026-11-11 a test of the old app (app/tests/test_cli.py, first-run setup) ran with no install folder
set, so it used the real default (%LOCALAPPDATA%\\SpeakStoneWorker) and its fake downloads left 1-byte placeholder
programs and an empty worker_config.json there. Now every test gets its own temporary home: the per-user folders all point
into it, and a test checks that the data folder really is inside it."""
import pytest


@pytest.fixture(autouse=True)
def _no_update_no_tray(monkeypatch):
    monkeypatch.setenv("SSW_NO_UPDATE", "1")
    monkeypatch.setenv("SSW_NO_TRAY", "1")
    monkeypatch.setenv("SSW_NO_SERVICE_WAKE", "1")     # opening the window in a test must never start the real service


@pytest.fixture(autouse=True)
def _sandbox_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    local = home / "AppData" / "Local"
    for key, value in {"LOCALAPPDATA": local, "APPDATA": home / "AppData" / "Roaming", "USERPROFILE": home, "HOME": home,
                       "XDG_DATA_HOME": home / ".local" / "share", "XDG_CONFIG_HOME": home / ".config"}.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv("SPEAKSTONE_API_BASE", raising=False)
    yield home
