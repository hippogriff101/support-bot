"""Slack Canvas persistence for resolved-ticket summaries.

Summaries are mirrored into a private canvas so human helpers can browse past
resolutions. The canvas is either supplied via `CANVAS_ID` or, if unset, the
bot attempts to create one (tabbed into the support channel) on first use and
remembers its ID in the DB. Everything degrades to `None`/`False` if the
canvas is unavailable or the app lacks `canvases:write` — AI summaries also
live in the DB, so the "similar question" feature keeps working regardless.
"""

import logging
import os

import db

CANVAS_ID = os.environ.get("CANVAS_ID")
CANVAS_SETTING = "support_canvas_id"


def resolve_canvas(client, channel_id: str) -> str | None:
    """Return the support-history canvas ID, creating one if needed."""
    canvas_id = CANVAS_ID or db.get_setting(CANVAS_SETTING)
    if canvas_id:
        return canvas_id
    try:
        created = client.canvases_create(
            title="Resolved support tickets",
            channel_id=channel_id,
            document_content={"type": "markdown", "markdown": "# Resolved support tickets\n"},
        )
        db.set_setting(CANVAS_SETTING, created["canvas_id"])
        logging.info("Created support-history canvas %s", created["canvas_id"])
        return created["canvas_id"]
    except Exception:
        logging.exception("Couldn't create support-history canvas (set CANVAS_ID to skip this)")
        return None


def append_summary(client, canvas_id: str, markdown: str) -> bool:
    """Append one summary markdown block to the end of a canvas."""
    try:
        client.canvases_edit(
            canvas_id=canvas_id,
            changes=[
                {
                    "operation": "insert_at_end",
                    "document_content": {"type": "markdown", "markdown": markdown + "\n"},
                }
            ],
        )
        return True
    except Exception:
        logging.exception("Couldn't append summary to canvas %s", canvas_id)
        return False
