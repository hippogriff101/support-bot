"""Test bootstrap.

`app.py` builds the Slack client and initialises the DB at import time, and
Bolt validates the token with a live `auth.test` call when it does. There is no
token (and no network) in tests, so the middleware initialisation is stubbed
out before any test module gets a chance to import `app`. DB_PATH points at a
scratch file so importing `app` never touches a real tickets.db.
"""

import os
import tempfile

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-token")
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(prefix="support-bot-tests-"), "import.db"))

import importlib  # noqa: E402

import pytest  # noqa: E402

try:
    from slack_bolt.app.app import App as _BoltApp

    # auth.test would need a real token; tests only ever call the handlers
    # directly, so the middleware (and the token check) are skipped.
    _BoltApp._init_middleware_list = lambda self, *args, **kwargs: None
except Exception:  # pragma: no cover - only if slack_bolt is missing
    pass


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point every store at tmp_path. db's functions bind DB_PATH as a default
    argument at import time, so it's reloaded with the env var set (and again
    afterwards to put the real default back)."""
    import db
    import helpers
    import macros

    monkeypatch.setenv("DB_PATH", str(tmp_path / "tickets.db"))
    importlib.reload(db)
    db.init_db()
    monkeypatch.setattr(helpers, "HELPERS_PATH", str(tmp_path / "helpers.json"))
    monkeypatch.setattr(macros, "MACROS_PATH", str(tmp_path / "macros.json"))
    macros.bootstrap_defaults()
    yield
    monkeypatch.delenv("DB_PATH")
    importlib.reload(db)
