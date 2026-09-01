"""Helper roster: who's allowed to act as support staff, and who's an admin.

Backed by a flat JSON file (helpers.json) rather than the ticket DB, since
it's meant to be hand-editable and event-specific:

    {
      "U078VN0UU2K": {"role": "Bento Sponsor", "admin": true}
    }

Re-read from disk on every lookup — the roster is tiny and rarely touched,
so this keeps admin edits (via /support or a manual file edit) visible
immediately without a bot restart.
"""

import json
import os

HELPERS_PATH = os.environ.get("HELPERS_FILE", "helpers.json")


def _load() -> dict:
    if not os.path.exists(HELPERS_PATH):
        return {}
    with open(HELPERS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    with open(HELPERS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def is_helper(user_id: str) -> bool:
    return user_id in _load()


def is_admin(user_id: str) -> bool:
    return _load().get(user_id, {}).get("admin", False)


def get_role(user_id: str) -> str | None:
    return _load().get(user_id, {}).get("role")


def add_helper(user_id: str, role: str, admin: bool = False) -> None:
    data = _load()
    existing = data.get(user_id, {})
    data[user_id] = {"role": role, "admin": admin or existing.get("admin", False)}
    _save(data)


def remove_helper(user_id: str) -> bool:
    data = _load()
    if user_id not in data:
        return False
    del data[user_id]
    _save(data)
    return True


def all_helpers() -> dict:
    return _load()
