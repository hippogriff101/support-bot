# Running an event

For admins: the people who set the bot up and look after the helper team.
The same material is in Slack (Home tab → **Admin guide**), and the Home
tab's **setup checklist** tracks your progress.

## Before the event

1. **Claim admin.** Start the bot, open its Home tab, click **Set up the
   bot**, and enter the setup code from the logs. On a host where you
   can't see logs, set `INITIAL_ADMIN_ID` to your Slack member ID instead
   (Profile → `⋯` → Copy member ID).
2. **Settings** (Home tab → ⚙️):
   - **Support channel:** a private channel with your helpers in it.
     `/invite` the bot first.
   - **Event name:** used in signatures ("— @Sam, Bento Support") and
     messages.
   - **Nudges:** how long a ticket can wait before the bot pings the
     channel. `0.5` is 12 hours, `0` turns it off.
   - The nudge's name and icon, and the **resolved emoji**. Custom
     workspace emoji work, without the colons.
3. **Add helpers:** Home tab → **Add helper**, or
   `/support promote @someone Role`. The role appears in their signature.
   Tick **Admin** only for people who should change settings and the
   roster.
4. **Ask every helper to take the training.** It's the **Start training**
   banner on their Home tab, about two minutes. The roster shows who's
   done it.
5. **Write your macros:** saved replies for the questions you'll get
   every day. `{name}`, `{event}` and `{date}` fill themselves in. Delete
   the starter macros you don't need.
6. **Tell people it exists.** Pin a message in your main channel: "Need
   help? DM @YourBot." Mention that messages to the bot are read by the
   support team.

## During the event

- **Waiting on a reply** on the Home tab (or `/support unanswered`) is the
  queue, longest-waiting first.
- Helpers **claim** what they're working on and **resolve** when it's
  answered. A ticket reopens by itself if the person writes again.
- **Nudges** post in the ticket's thread and the channel, and ping whoever
  claimed it.
- The pinned **Support stats** message in the channel shows open vs
  resolved.
- Need to contact someone first, like a winner or someone whose submission
  is missing something? Use `?@them`. See the helper guide.
- **Don't switch the support channel mid-event** unless you have to. Open
  tickets stay in the old channel and replies there stop relaying.

## After the event

- Resolve or close what's left. Anything still open shows up again next
  event if you reuse the bot.
- Thank your helpers! The Home tab leaderboard shows replies and
  resolutions per person.
- Decide what data to keep (see below), then remove the rest.

## Data and privacy

What the bot stores, all on the server you host it on:

| Where | What |
|---|---|
| `tickets.db` (SQLite) | Each ticket's first message, IDs and timestamps of relayed messages, every reply's text, claim/resolve history, settings, and who finished training. Edit/delete log lines live in Slack threads, not here. |
| `helpers.json` | Roster: Slack IDs, roles, admin flag. |
| `macros.json` | Saved replies. |

- Messages also stay in Slack, in the DMs and the support channel, under
  your workspace's retention settings.
- The bot sends nothing to AI services, analytics, or anyone other than
  Slack.
- Anyone with access to the server or the support channel can read
  tickets. Keep the support channel private and the helper list tight.
- To erase everything, stop the bot and delete `tickets.db` (and
  `helpers.json` if you're done with the roster). Slack messages need
  deleting separately.
- Tell people what happens to their messages. A line in your announcement
  is enough: "Messages to the bot are read by the organising team and kept
  until <date>."

## Commands for admins

| Command | |
|---|---|
| `/support config` | Open Settings |
| `/support promote @user [role]` | Add or update a helper |
| `/support superpromote @user [role]` | Same, as admin |
| `/support demote @user` | Remove from the roster (not the last admin) |
| `/support macros <name> <text>` | Add or update a macro |
| `/support macros delete <name>` | Delete a macro |
| `/support guide admin` | The admin handbook in Slack |

Everything else is in [how-it-works.md](how-it-works.md).
