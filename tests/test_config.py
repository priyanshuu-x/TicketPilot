from ticketpilot.config import Settings


def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("MAX_STEPS", "5")
    assert Settings().max_steps == 5
