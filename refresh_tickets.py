"""One-off: re-render every ticket's forwarded message in the support channel
so its buttons match its state (Claim/Resolve while open, Reopen once
resolved). Tickets from before the buttons existed have none at all.

    python refresh_tickets.py           # dry run — shows what would change
    python refresh_tickets.py --apply   # actually update the messages

Safe to re-run: it only rewrites each message's buttons/status line, never
its text, and skips tickets whose message is already up to date.
"""

import sys
import time

from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

import app
import config
import db


def main(apply: bool) -> None:
    client = app.app.client
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=5))
    channel = config.support_channel_id()
    tickets = db.all_tickets()
    print(f"{len(tickets)} tickets in {channel} ({'APPLYING' if apply else 'dry run'})")

    changed = 0
    for ticket in tickets:
        try:
            parent = client.conversations_replies(channel=channel, ts=ticket["support_ts"], limit=1)["messages"][0]
        except Exception as e:
            print(f"  #{ticket['id']}: couldn't read message ({e}) — skipped")
            continue
        text = ticket["support_text"] or parent.get("text", "")
        wanted = app._ticket_blocks(text, ticket)
        current_buttons = [
            e.get("action_id") for b in parent.get("blocks", []) if b["type"] == "actions" for e in b["elements"]
        ]
        wanted_buttons = [e["action_id"] for b in wanted if b["type"] == "actions" for e in b["elements"]]
        if current_buttons == wanted_buttons and ticket["support_text"] is not None:
            continue

        changed += 1
        print(f"  #{ticket['id']} {ticket['status']:6} {current_buttons or 'no buttons'} -> {wanted_buttons}")
        if apply:
            if ticket["support_text"] is None:
                db.set_ticket_text(ticket["support_ts"], text)
            app.refresh_ticket_message(client, ticket["support_ts"])
            time.sleep(1.2)  # chat.update is rate-limited to ~50/min

    print(f"{changed} message(s) {'updated' if apply else 'would be updated'}.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
