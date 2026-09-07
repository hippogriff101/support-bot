# Event Support Bot

A Slack bot for event support: DM the bot → confirm → forwarded to a private
support channel → a helper replies with a `?` prefix → relayed back to the
user. Everything event-specific (channel, roster, event name) is config, not
code, so the same bot works for any Slack-run event.

> [!WARNING]
> This app has been made by Claude Code with assistance by me (freddie) for the YSWS Bento!

## 1. Create the Slack app

Go to https://api.slack.com/apps → **Create New App** → From scratch.

- **Socket Mode**: turn it on, create an app-level token with the
  `connections:write` scope → that's `SLACK_APP_TOKEN`.
- **Bot Token Scopes**: `chat:write`, `im:history`, `im:read`,
  `groups:history` (use `channels:history` instead if your support channel
  is public), `reactions:write`, `users:read`, `commands`.
- **Event Subscriptions**: `message.im`, `message.groups` (or
  `message.channels` if public).
- **Interactivity & Shortcuts**: turn it on, add two "On messages" shortcuts
  — **Edit reply** (`edit_reply`) and **Delete reply** (`delete_reply`).
- **Slash Commands**: add `/support`.
- **Install App to Workspace**, copy the Bot Token (`xoxb-...`) → that's
  `SLACK_BOT_TOKEN`. Invite the bot into your support channel.

## 2. Configure

```bash
cp .env.example .env
```

Fill in `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SUPPORT_CHANNEL_ID`, and
`EVENT_NAME`.

Add at least one admin to `helpers.json` (git-ignored — real roster data):

```json
{ "U0123ABCDEF": { "role": "Event Support", "admin": true } }
```

No filesystem access before the first deploy (e.g. Orchard)? Set
`INITIAL_ADMIN_ID` instead — it seeds that one admin on startup if the
roster's empty. Use `/support promote` from there to add everyone else.

`macros.json` ships in the repo with a few generic starter canned replies —
edit by hand or manage live with `/support macros <name> <reply text>`.

## 3. Run

```bash
pip install -r requirements.txt
python app.py
```

### Deploying

This is a long-running Socket Mode process — no public URL or webhook
needed, so any host that keeps a Python process alive works. Give
`tickets.db` and `helpers.json` a persistent volume (they reset on redeploy
otherwise — `helpers.json` back down to just the seeded admin). `macros.json`
ships with defaults, so it's fine to leave unpersisted; if you do persist it,
`bootstrap_defaults()` reseeds the defaults on first boot so an empty volume
doesn't wipe them. Point `DB_PATH` / `HELPERS_FILE` / `MACROS_FILE` at the
volume instead of shipping a `.env` file.

On startup the bot posts "✅ {EVENT_NAME} support bot is online." to the
support channel, so you can tell at a glance after a deploy/restart that it
actually came back up.

## How it behaves

- DM the bot → 👀 reaction → "send to support?" prompt. Confirming forwards
  it to the support channel and marks the DM ✅.
- Once a ticket exists, further replies in that DM thread relay straight
  into the ticket's thread — no repeat prompt.
- A helper's `?text` reply in a ticket thread relays back to the user,
  signed with their Slack mention and their own role from `helpers.json`
  (e.g. `— @Helper, Clubs Team`), falling back to `{EVENT_NAME} Support` if
  they somehow don't have one set. `?macro-name` sends that macro's canned
  text instead. Only users in `helpers.json` can do this.
- `?close` resolves a ticket (tick reaction, helper-only note, no DM to the
  user). Any new activity — the user replying, or another `?` reply —
  reopens it.
- A pinned **Support stats** message tracks Open vs Resolved counts.

### Commands

- `/support whoami` — see your own role.
- `/support list` — helpers only: list the roster.
- `/support macros [name] [reply text]` — list macros, or (admins) add/update one.
- `/support promote @user [role]` — admins only: add/update a helper. Role
  defaults to `{EVENT_NAME} Support` if omitted; never demotes an existing
  admin.
- `/support superpromote @user [role]` — admins only: same as `promote`, but
  also grants admin.
- `/support demote @user` — admins only: remove someone from the roster
  entirely.

### Message shortcuts

On a helper's own `?`-reply message: **Edit reply** and **Delete reply**,
usable by the original sender or an admin.

## Notes

- Replies are tagged with the helper's Slack mention and role rather than
  posted *as* them, to avoid needing `chat:write.customize` and faking
  identities.
- A user can open multiple tickets — each gets its own DM/support thread.
- In-flight "Yes/No" confirmation buttons don't survive a bot restart
  (open tickets do, via SQLite).
- Any helper can answer any ticket — no claim/ack step.

## License

MIT — see [LICENSE](LICENSE).
