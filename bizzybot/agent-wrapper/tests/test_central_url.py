"""Run with: uv run --with pytest -m pytest tests

Which Central-Dispatch the wrapper dials: CENTRAL_URL, then the saved config,
then the default. A saved config naming a retired address uses the default.
"""

from bizzybot_agent_wrapper import agent_wrapper as aw

RAILWAY = "https://claudebot-production-34ba.up.railway.app"


def resolve(monkeypatch, saved=None, env=None):
    monkeypatch.setattr(aw, "_load_local_config", lambda: {"central_url": saved} if saved else {})
    if env:
        monkeypatch.setenv("CENTRAL_URL", env)
    else:
        monkeypatch.delenv("CENTRAL_URL", raising=False)
    return aw.resolve_central_dispatch()


def test_default_when_nothing_saved(monkeypatch):
    assert resolve(monkeypatch) == aw.DEFAULT_CENTRAL_URL


def test_retired_saved_url_uses_default(monkeypatch):
    assert resolve(monkeypatch, saved=RAILWAY) == aw.DEFAULT_CENTRAL_URL
    assert resolve(monkeypatch, saved=RAILWAY + "/") == aw.DEFAULT_CENTRAL_URL


def test_other_saved_url_kept(monkeypatch):
    assert resolve(monkeypatch, saved="http://localhost:3000/") == "http://localhost:3000"


def test_env_wins(monkeypatch):
    assert resolve(monkeypatch, saved=RAILWAY, env="https://example.test") == "https://example.test"
