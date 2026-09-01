"""Canned reply macros: a helper typing exactly `?<name>` in a ticket thread
sends the canned text for `<name>` instead of relaying `<name>` literally.

Backed by a flat JSON file (macros.json), tracked in git with a few generic
defaults (unlike helpers.json/tickets.db, there's nothing event-specific or
secret in a macro's text) but still editable per event, either by hand or
via `/support macros <name> <reply text>`:

    {
      "review": "Hi! Thanks for messaging us! We have seen your project and it is awaiting review - if it has been longer than 7 days then please send another message and we will get right on it!"
    }

Re-read from disk on every lookup so an edit takes effect immediately, same
as helpers.json.
"""

import json
import os

MACROS_PATH = os.environ.get("MACROS_FILE", "macros.json")


def _load() -> dict:
    if not os.path.exists(MACROS_PATH):
        return {}
    with open(MACROS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    with open(MACROS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def get_macro(name: str) -> str | None:
    name = name.strip().lower()
    for key, text in _load().items():
        if key.strip().lower() == name:
            return text
    return None


def all_macros() -> dict:
    return _load()


def add_macro(name: str, text: str) -> None:
    data = _load()
    data[name.strip().lower()] = text
    _save(data)
