# How it works

Everything the bot does, in detail. For a quick overview see the
[README](../README.md); for day-to-day use, the in-Slack helper training
(Home tab → **Helper guide**) is shorter.

## Tickets, replies and files

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
- `?close` resolves a ticket (a `RESOLVED_EMOJI` reaction, helper-only note, no DM to the
  user). Any new activity — the user replying, or another `?` reply —
  reopens it.
- `?claim` in a ticket thread records which helper is handling it — a
  helper-only note in the thread, nothing to the user, and it doesn't stop
  anyone else from replying (see [Notes](#notes)). Purely for stats/context.
  `?unclaim` lets it go again.
- **`?@user` starts a DM with someone** — type it in a ticket thread, or as a
  message of its own in the channel; both work. `?<@U123>`, `? <@U123>` and
  `?@U123` all work (the `@` is
  required, so a reply like `?WAIT` is never mistaken for an ID); the offer
  prompt names both people (`@you is setting up a message thread with
  @them. Continue?`) so a mistyped ID is obvious before anything is sent.
  The prompt goes in the ticket's thread when you typed it in one, or in a
  new thread under your message when you posted it on its own. Once the
  modal's sent, the prompt turns into a link to the new ticket (so it can't
  be clicked twice). A helper's `?…` in a thread that isn't a ticket, or a
  top-level `?close`/`?claim`/macro/file, gets a private "that wasn't sent
  to anyone" note; other top-level messages (`?? is the bot down`) and
  non-helpers' top-level messages are left alone. Files attached to a
  `?@user` aren't carried into the DM, and you're told so.
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
- The bot's **Home tab** (click its name/icon in Slack):
  - **Anyone who isn't a helper** sees how to ask for help and their own
    recent requests, each marked waiting, being handled or resolved, with
    links back to the DM threads.
  - **Helpers** see Open/Resolved counts, the unanswered queue, their own
    claimed tickets and reply/resolve counts, a per-helper leaderboard,
    anything they've scheduled (queued `?@user` messages), and the
    **Helper guide**. That's a seven-page training with Back/Next buttons.
    Until a helper finishes it, a "New here?" banner offers it.
    `/support guide` opens it too.
  - **Admins** also get the **Admin guide** (a handbook), a **setup
    checklist** that ticks itself off and then disappears, each helper's
    resolved count and training status, org-wide numbers, **Settings**
    (support channel, event name, nudges, resolved emoji), and
    add/edit/remove for **helpers** and **macros**.

### Commands

`/support` (or `/support help`) lists everything.

- `/support guide` — helpers: the helper training. `/support guide admin`
  opens the admin handbook (admins). Anyone else gets the "how to get help"
  steps.
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

### Settings reference

Each setting can be set in the environment *or* from the Home tab's
**Settings** (admins) — a value saved in Slack wins.

| Env var | Default | What it does |
|---|---|---|
| `SLACK_BOT_TOKEN` | — | **Required.** The `xoxb-` bot token. |
| `SLACK_APP_TOKEN` | — | **Required.** The `xapp-` app-level token (Socket Mode). |
| `SUPPORT_CHANNEL_ID` | — | Channel tickets go to. Easier to pick from Settings. |
| `EVENT_NAME` | `Event` | Shown in signatures (`{EVENT_NAME} Support`) and messages. |
| `STALE_DAYS` | `2` | Nudge after this many days without a reply. `0` = off. |
| `NUDGE_NAME` / `NUDGE_ICON` | `Chopsticks` / `:chopsticks:` | Who the nudges and log lines post as. |
| `RESOLVED_EMOJI` | `white_check_mark` | Reaction on resolved tickets — any emoji name, including your workspace's custom ones. |
| `INITIAL_ADMIN_ID` / `INITIAL_ADMIN_ROLE` | — | Seed the first admin instead of using the setup code. |
| `DB_PATH` | `tickets.db` | SQLite database (tickets, replies, logs, settings). |
| `HELPERS_FILE` | `helpers.json` | The helper roster. |
| `MACROS_FILE` | `macros.json` | Saved replies. Seeded with generic defaults if missing. |
| `PORT` | — | If set, answer HTTP health checks on it (`GET /` → `ok`). |

On startup the bot posts "{EVENT_NAME} support bot has been deployed
successfully." to the support channel, so you can tell a restart worked.

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

