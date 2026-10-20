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
- **Bot Token Scopes**: `chat:write`, `chat:write.customize`, `im:history`,
  `im:read`, `im:write`, `groups:history` (use `channels:history` instead if
  your support channel is public), `reactions:write`, `users:read`,
  `commands`, `files:read`, `files:write`.
  `chat:write.customize` lets the bot post a helper's/user's own name and
  profile picture on relayed messages instead of the bot's — if you add
  this scope to an already-installed app, reinstall it to the workspace to
  pick up the new permission. `files:read`/`files:write` are what let the
  bot copy attachments between the user's DM and the support thread.
  `im:write` is what lets a helper's `?@user` open a DM with someone who
  hasn't DMed the bot first.
- **Event Subscriptions**: `message.im`, `message.groups` (or
  `message.channels` if public), `app_home_opened`.
- **App Home**: turn on the **Home Tab** (Messages Tab can stay off) — this
  is where first-time setup, the stats page and the admin dashboard live.
- **Interactivity & Shortcuts**: turn it on, add two "On messages" shortcuts
  — **Edit reply** (`edit_reply`) and **Delete reply** (`delete_reply`).
- **Slash Commands**: add `/support`.
- **Install App to Workspace**, copy the Bot Token (`xoxb-...`) → that's
  `SLACK_BOT_TOKEN`. Invite the bot into your support channel.

## 2. Configure

```bash
cp .env.example .env
```

Only `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are required. Everything else
(support channel, event name, nudges) can live in `.env` *or* be set by an
admin from the bot's **Home tab → Settings** — a value saved there wins over
`.env`, so you can change it mid-event without a redeploy.

### First admin

The easy way: start the bot, then open its **Home tab** in Slack. While
nobody's an admin it shows **Set up the bot** — click it and enter the
one-time setup code from the bot's logs:

```
WARNING:root:No admins yet — open the bot's Home tab in Slack, click 'Set up the bot' and enter setup code: 1a2b3c4d
```

You're now an admin and it drops you straight into Settings (pick the
support channel there — invite the bot to it first). The code only lives in
the logs so that being first to open the Home tab isn't enough to grab admin
in a big workspace.

Alternatively, set `INITIAL_ADMIN_ID` (seeds that one admin on startup if
the roster's empty), or hand-write `helpers.json` (git-ignored — real roster
data):

```json
{ "U0123ABCDEF": { "role": "Event Support", "admin": true } }
```

Add everyone else from the Home tab's admin dashboard or `/support promote`.

`macros.json` ships in the repo with a few generic starter canned replies —
manage them from the Home tab, with `/support macros`, or by hand.

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
  it to the support channel and marks the DM ✅. The forwarded message's
  buttons follow the ticket's state: **Claim** + **Resolve** while it's
  open, **Unclaim** + **Resolve** (plus a "Claimed by" line) once claimed,
  and **Reopen** (plus "Resolved by") once resolved. A channel message's
  buttons are shared by everyone who can see it, so they show the ticket's
  state; whoever actually claimed it is the one who can unclaim (anyone else
  gets a private "X already has this one").
- **Attachments** move both ways, but the bot asks first: a user sending a
  file as a follow-up gets a "send this to the support team?" prompt, and
  a helper dropping a file into a ticket thread *without* `?` gets a
  private "send this to the user?" prompt (only the files go — the text is
  taken as internal). The prompt names each file as a link, so it's clear
  what's about to be sent; anything already deleted shows as a tombstone
  and is skipped. Files in the user's first message ride along with its
  usual prompt, and a `?` reply with files sends them straight away (a bare
  `?` just sends the files). Copies are uploaded by the bot itself — Slack
  doesn't allow a custom name/photo on file uploads — so the message next to
  them is what says who sent them. Anything that fails to copy gets a note
  in the support thread.
- **Edits and deletes follow the message**, and every one is logged in the
  ticket's support thread naming who did it without pinging them:
  - a helper editing their `?` reply updates the user's copy; deleting it,
    or editing the `?` away, deletes the user's copy.
  - a user editing their original message updates the forwarded ticket
    message; editing/deleting a follow-up updates/deletes its copy in the
    thread. Deleting the original is logged but the ticket message stays,
    since the whole thread hangs off it.
  - the log line carries both timestamps, so a long gap between sending and
    editing isn't lost: `<name> deleted their message: "…", sent at
    <time>, deleted at <time>` and `<name> edited their message. It
    originally was sent at <time>, and was originally "…". It was edited at
    <time> to "…"`. Times are UTC.
- Once a ticket exists, further replies in that DM thread relay straight
  into the ticket's thread — posted under the user's own Slack name/photo
  (the opening `*Name* (@user):` line stays too) — no repeat prompt.
- A helper's `?text` reply in a ticket thread relays back to the user,
  posted under the helper's own Slack name/photo and signed with their
  Slack mention and their own role from `helpers.json` (e.g.
  `— @Helper, Clubs Team`), falling back to `{EVENT_NAME} Support` if they
  somehow don't have one set. `?macro-name` sends that macro's canned text
  instead. Only users in `helpers.json` can do this.
- `?close` resolves a ticket (tick reaction, helper-only note, no DM to the
  user). Any new activity — the user replying, or another `?` reply —
  reopens it.
- `?claim` in a ticket thread records which helper is handling it — a
  helper-only note in the thread, nothing to the user, and it doesn't stop
  anyone else from replying (see [Notes](#notes)). Purely for stats/context.
  `?unclaim` lets it go again.
- **`?@user` starts a DM with someone** — inside the thread it offers to set
  one up. `?<@U123>`, `? <@U123>` and `?@U123` all work (the `@` is
  required, so a reply like `?WAIT` is never mistaken for an ID); the offer
  prompt names both people (`@you is setting up a message thread with
  @them. Continue?`) so a mistyped ID is obvious before anything is sent.
  Continuing opens a modal for the first message and an optional delay (up
  to 48 hours). The DM opens with a "… will send you a message here <when>"
  line, and the conversation gets **its own ticket** in the support channel,
  claimed by the helper who started it. From there it's an ordinary ticket:
  the first message (and every later `?…` reply in the new ticket's thread)
  lands in the DM thread, the recipient's replies there relay back, and
  `?claim`/`?unclaim`/`?close` all work. If the scheduled message can't be
  sent, the helper is pinged in that ticket's thread.
- **Unanswered tickets**: a ticket is "waiting" while it's open and the
  user's latest message hasn't had a `?` reply since. `/support unanswered`
  and the Home tab list them longest-waiting first, with links.
- **Nudges**: once a ticket's been waiting `STALE_DAYS` (default 2, set in
  `.env` or Home → Settings), the bot posts in its thread *and* to the
  channel as **Chopsticks** 🥢 (name/icon configurable), pinging whoever
  claimed it. It checks every 10 minutes and won't nudge the same ticket
  again until another full period has passed.
- A pinned **Support stats** message in the support channel tracks Open vs
  Resolved counts.
- The bot's **Home tab** (click its name/icon in Slack): anyone who isn't
  a helper just sees a "DM me for help" notice. Helpers see Open/Resolved
  counts, the unanswered queue, a per-helper reply-count leaderboard, and
  anything they've scheduled (queued `?@user` messages). Admins get the
  **admin dashboard** on top of that:
  each helper's resolved count, org-wide numbers, **Settings** (support
  channel, event name, nudge timing/name/icon), and
  add/edit/remove for **helpers** and **macros**.

### Commands

`/support` (or `/support help`) lists everything.

- `/support whoami` — see your own role.
- `/support list` — helpers only: list the roster.
- `/support unanswered` — helpers only: open tickets still waiting on a
  reply, longest first.
- `/support macros` (or `/support macros list`) — helpers only: every macro
  and its full text.
- `/support macros <name> <reply text>` — admins only: add/update a macro.
- `/support macros delete <name>` — admins only: delete a macro.
- `/support config` — admins only: open the Settings modal.
- `/support promote @user [role]` — admins only: add/update a helper. Role
  defaults to `{EVENT_NAME} Support` if omitted; never demotes an existing
  admin.
- `/support superpromote @user [role]` — admins only: same as `promote`, but
  also grants admin.
- `/support demote @user` — admins only: remove someone from the roster
  entirely. Refuses to remove the last admin.

### Message shortcuts

On a helper's own `?`-reply message: **Edit reply** and **Delete reply**,
usable by the original sender or an admin. Editing only changes the text —
Slack doesn't support changing a message's displayed name/photo after it's
posted, so an edited reply keeps showing whoever sent the original. Both
are logged in the thread, same as editing/deleting the message directly.

### Upgrading an existing deploy

Tickets forwarded before the state-following buttons existed have no
buttons. Once the new version's running, give them the right ones:

```bash
python refresh_tickets.py           # dry run — lists what would change
python refresh_tickets.py --apply   # update the messages
```

It only touches each message's buttons/status line (never its text) and is
safe to re-run.

New tables (`outbox`, `scheduled_dms`) are created by `init_db()`
on startup, so the upgrade needs no manual migration.

## Development

```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/pytest -q          # 85 tests
./.venv/bin/ruff check .       # lint
./.venv/bin/ruff format .      # format
```

The tests import `app.py`, which builds the Slack client at import time, so
`tests/conftest.py` sets dummy tokens and stubs Bolt's token check — no
network or real workspace needed. `tests/test_new_features.py` covers the
pure logic (delay parsing, placeholder rendering, block building, DB
round-trips); `tests/test_flows.py` drives the real handlers with a fake
client (file prompts, `?@user` scheduling). `tests/test_no_regressions.py`
pins the behaviour upstream already had.

## Notes

- Relayed messages use `chat:write.customize` to show the sender's own
  Slack name/photo, but are still posted *by the bot* — Slack marks them
  "(via {App Name})" and they can't trigger @-mentions/notifications for
  that person the way a real message from them would. The text signature
  (helper's mention + role in DMs, the `*Name* (@user):` line in the
  support thread) stays as the actual source of truth for who sent what.
- A user can open multiple tickets — each gets its own DM/support thread.
- The "send to support?" prompt re-reads the user's message when they
  confirm, so long messages and attachments forward fine, and a double-click
  doesn't open two tickets.
- Changing the support channel mid-event moves the stats message over, but
  open tickets stay in the old channel's threads — helper replies there
  won't relay any more, so finish them off first.
- `?claim` is informational only, not enforcement — any helper can still
  answer any ticket even after someone else claims it. It exists so stats
  can attribute a ticket to whoever's handling it, not to gate replies.

## License

MIT — see [LICENSE](LICENSE).
