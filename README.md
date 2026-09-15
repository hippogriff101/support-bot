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
- **Bot Token Scopes**: `chat:write`, `chat:write.customize` (per-message
  helper name/PFP), `files:write` (image forwarding), `canvases:read` +
  `canvases:write` (summary canvas), `im:history`, `im:read`,
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

For the AI/canvas features, create a **Hack Club AI API key** at
https://ai.hackclub.com and drop it in `.env`. Full config reference lives
in `.env.example` — `HACK_CLUB_AI_API_KEY`, `AI_MODEL`, `CANVAS_ID`,
`SUPPORT_TEAM_NAME`. The bot runs fine without any of them; the extra
features just stay off.

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
- Helper replies are posted **with the helper's own name and avatar** — the
  user sees `《Firstname》| <event> Support Team` with that helper's Hack Club
  PFP — rather than the generic bot identity. Falls back to the bot identity
  if the workspace rejects the override.
- **Images**: attachments travel both ways. A user's photo in the DM lands in
  the ticket thread; a helper's `?` reply with an image forwards it to the
  user. A bare `?` with just an image still relays the image.
- `?close` resolves a ticket (tick reaction, helper-only note, no DM to the
  user). On close the bot asks **Hack Club AI** for a one-paragraph Q&A
  summary, appends it to the support canvas (see `CANVAS_ID`) and keeps it in
  the DB. Any new activity — the user replying, or another `?` reply —
  reopens it.
- When a new ticket is opened, the AI checks past summaries for a **similar
  question**; if one matches, the ticket thread gets a `Send to user` button
  with an AI-suggested reply. Hitting it relays the suggestion to the user,
  signed with whichever helper clicked it.
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

- Replies are tagged with the helper's role (and shown with their name/PFP via
  `chat:write.customize`) rather than posted *as* them, to avoid faking
  identities.
- Picture/identity PFP URLs come from Hack Club's public cachet service
  (`https://cachet.hackclub.com/users/<slack-id>/r`).
- The AI + canvas features need `HACK_CLUB_AI_API_KEY`; if it's missing the
  bot just skips summaries/suggestions. Canvas writes need `CANVAS_ID` (or the
  `canvases:write` scope to auto-create one).
- Image forwarding re-uploads attachments with the bot's token — nothing is
  made public; single-file failures are skipped.
- A user can open multiple tickets — each gets its own DM/support thread.
- In-flight "Yes/No" confirmation buttons don't survive a bot restart
  (open tickets do, via SQLite).
- Any helper can answer any ticket — no claim/ack step.

## License

MIT — see [LICENSE](LICENSE).
