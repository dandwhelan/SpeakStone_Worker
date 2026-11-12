"""No test of the old app may touch a real install.

On 2026-11-11 app/tests/test_cli.py::test_first_run_sets_up_then_shows_the_menu ran with no install folder set, so it used
the real default (%LOCALAPPDATA%\\SpeakStoneWorker) and its fake downloads left 1-byte placeholder programs and an empty
worker_config.json there. Every test now gets its own temporary home, and a test checks that the default install folder
is inside it."""
import pytest


@pytest.fixture(autouse=True)
def _sandbox_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    for key, value in {"LOCALAPPDATA": home / "AppData" / "Local", "APPDATA": home / "AppData" / "Roaming",
                       "USERPROFILE": home, "HOME": home, "XDG_DATA_HOME": home / ".local" / "share",
                       "XDG_CONFIG_HOME": home / ".config"}.items():
        monkeypatch.setenv(key, str(value))
    yield home
