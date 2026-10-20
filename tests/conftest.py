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

try:
    from slack_bolt.app.app import App as _BoltApp

    # auth.test would need a real token; tests only ever call the handlers
    # directly, so the middleware (and the token check) are skipped.
    _BoltApp._init_middleware_list = lambda self, *args, **kwargs: None
except Exception:  # pragma: no cover - only if slack_bolt is missing
    pass
