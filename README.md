# Event Support Bot

Slack Bolt (Python), Socket Mode. DM the bot → confirm → forwarded to a
private support channel → helper replies with a `?` prefix → relayed back
to the user in the original DM thread.

A generic, reusable support-ticket bot — everything event-specific
(support channel, roster, event name) is config, not code, so the same
bot works for any Slack-run event.

## 1. Create the Slack app

Go to https://api.slack.com/apps → **Create New App** → From scratch.

### Socket Mode
- **Settings → Socket Mode** → toggle on.
- This generates an **app-level token** — create one with the `connections:write`
  scope. That's your `SLACK_APP_TOKEN` (starts with `xapp-`).

### Bot Token Scopes (OAuth & Permissions → Scopes → Bot Token Scopes)
- `chat:write` — post messages
- `im:history` — read DM messages
- `im:read` — see DM channel info
- `groups:history` — read messages in the (private) support channel
- `reactions:write` — add ✅ / 👀 / ❌ reactions
- `users:read` — look up real names for signatures
- `commands` — added automatically once you create a slash command below

> If your support channel is public instead of private, use `channels:history`
> instead of `groups:history`.

### Event Subscriptions (Event Subscriptions → Subscribe to bot events)
- `message.im`
- `message.groups` (or `message.channels` if the support channel is public)

### Interactivity & Shortcuts
- **Interactivity & Shortcuts** → toggle on. (No Request URL needed — Socket
  Mode handles this.)
- Under **Shortcuts** → **Create New Shortcut** → **On messages**, add two:
  - **Edit reply** — callback ID `edit_reply`
  - **Delete reply** — callback ID `delete_reply`

  These appear on the "..." menu of a message in Slack. They only do
  anything on a helper's own `?`-reply message in the support channel (see
  [Message shortcuts](#message-shortcuts) below).

### Slash Commands (Slash Commands → Create New Command)
No Request URL needed — Socket Mode handles this too.
- `/support` — usage hint: `promote @user <role> | demote @user | list | whoami`

### Install
- **Install App to Workspace**. Copy the **Bot User OAuth Token**
  (`xoxb-...`) — that's your `SLACK_BOT_TOKEN`.
- Invite the bot into your private support channel: `/invite @YourBotName`.

## 2. Configure

```bash
cp .env.example .env
```

Fill in:
- `SLACK_BOT_TOKEN` — the `xoxb-...` token
- `SLACK_APP_TOKEN` — the `xapp-...` token
- `SUPPORT_CHANNEL_ID` — right-click the channel → View channel details →
  copy the Channel ID at the bottom
- `EVENT_NAME` — used in the signature line sent back to users

Then set up `helpers.json` (git-ignore this per event, it's real roster data):

```json
{
  "U0123ABCDEF": { "role": "Event Support", "admin": true }
}
```

At least one entry needs `"admin": true` — that's who can run `/support
promote` / `/support demote` to grow the roster from there. Get a user's ID
via their Slack profile → **More** → **Copy member ID**.

## 3. Run

```bash
pip install -r requirements.txt
python app.py
```

`tickets.db` (SQLite) is created automatically on first run in the same
directory. Back it up if you care about ticket history — it's the only
state the bot keeps.

### Deploying

This is a long-running process (Socket Mode keeps a persistent connection —
no public URL or webhook needed), so any host that runs a persistent Python
process works: a small VM, a container platform, etc. The only thing that
needs to survive restarts/redeploys is `tickets.db`, so give it a persistent
volume/disk rather than ephemeral container storage, and set the env vars
from `.env.example` (plus `HELPERS_FILE` if you don't want `helpers.json` in
the default location) directly in the platform's env var config instead of
shipping a `.env` file.

## How it behaves

- User DMs the bot → bot reacts 👀 and asks (in-thread) whether to send it
  to support.
- "Yes, send it" → original DM message gets a ✅, message is posted to the
  support channel as `*Name* (@user) > message`.
- Anyone who posts in the support channel who **isn't in `helpers.json`**
  gets a ❌ reaction and an ephemeral "you don't have permission" notice
  (visible only to them) — nothing is relayed or logged.
- A helper replies **in a ticket thread** in the support channel:
  - starting with `?` → relayed back to the user in their DM thread, signed
    `— Helper's first name, {EVENT_NAME} Support`. Helper's message gets a ✅.
  - anything else → ignored (treated as internal helper chatter, not
    intended for the user).
- A helper posting outside a ticket thread (e.g. general channel chatter) is
  just left alone — the permission gate above is the only check that applies.

### Commands

- `/support whoami` — anyone: see your own role, if you have one.
- `/support list` — helpers only: list the current roster.
- `/support promote @user <role>` — admins only: add someone to
  `helpers.json`, or update their role if they're already in it. `<role>` is
  optional — defaults to `{EVENT_NAME} Support`.
- `/support demote @user` — admins only: remove someone from the roster.

### Message shortcuts

Available from the "..." menu on any message in the support channel, but
only act on a tracked `?`-reply message (see [Configure](#interactivity--shortcuts)
for setting these up in the Slack app config):

- **Edit reply** — opens a modal with the reply text; saving updates the
  copy already sent to the user in their DM and adds a ✏️ reaction to the
  support-channel message. Usable by the original sender or an admin.
- **Delete reply** — deletes the message from the user's DM and swaps the
  support-channel message's ✅ for a ❌. Usable by the original sender or an
  admin.

Both no-op with an ephemeral notice if the message isn't a tracked reply, was
already deleted, or the caller isn't the sender/an admin.

## Notes / things you may want to adjust

- **Signature vs. impersonation**: this deliberately signs replies with the
  helper's real first name rather than posting *as* them (which would need
  `chat:write.customize` + faking their name/avatar). Signing keeps it
  honest about who's actually posting, and doesn't break if a helper's
  Slack profile changes mid-event.
- **Multiple tickets per user**: since each ticket is keyed off the user's
  original DM `ts`, a user can open several tickets — each lives in its own
  DM thread and its own support-channel thread, no collisions.
- **Restarts**: SQLite means open tickets survive a bot restart. In-flight
  "Yes/No" confirmation buttons do NOT survive a restart (their state lives
  in the button's `value`, not the DB) — if that matters for your event,
  say so and I'll persist those too.
- **No claim/ack step**: any helper in the channel can answer any ticket.
  Easy to add a "claim" button if multiple helpers might collide on the
  same ticket.
- **`helpers.json` reload**: it's re-read from disk on every check, so a
  manual edit (or a `/support promote`) takes effect immediately — no
  restart needed.
- **Other commands worth adding, if useful**:
  - `/support claim` (in a ticket thread) — assign a ticket to the
    replying helper so others know it's being handled; pairs well with the
    "claim/ack" note above.
  - `/support close` (in a ticket thread) — mark a ticket resolved in the
    DB and post a closing note to the user, instead of leaving tickets
    open forever.
  - `/support stats` — quick counts (open tickets, tickets today, per
    helper) for wrapping up an event.

## License

MIT — see [LICENSE](LICENSE).
