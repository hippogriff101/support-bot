# Helper cheat sheet

The full training is in Slack: open the bot's **Home** tab and click
**Start training** (or **Helper guide**), or type `/support guide`. This
page is the one-screen version to keep open during an event.

## The one rule

**Reply in the ticket's thread, and start with `?` to send it to the
person.** Without `?`, only the support team sees it.

## In a ticket's thread

| Type | Does |
|---|---|
| `?Hi! Try restarting it.` | Sends to the person, signed with your name and role |
| `?Hi {name}, …` | `{name}` becomes their first name |
| `?macro-name` | Sends a saved reply (`/support macros` lists them) |
| *(no `?`)* `@sam can you check this?` | Internal note, team only |
| `?claim` / `?unclaim` | "I've got this" / let it go |
| `?close` | Resolve (they aren't messaged) |
| `?` + a file | Sends the file |
| a file, no `?` | The bot privately asks whether to send it |

Or use the ticket's **Claim**, **Resolve** and **Reopen** buttons.

## Fixing a mistake

- **Edit** your `?` message, and their copy updates.
- **Delete** it, or edit the `?` away, and their copy disappears.
- Everything is logged in the thread with times (UTC).

## Messaging someone first

`?@their-name`, in a ticket's thread or on its own in the support channel
→ **Continue** → write the message and when to send it (up to 48h). It
becomes its own ticket, claimed by you.

## Good habits

- **Acknowledge fast.** "Looking into it!" beats silence.
- **Claim before digging in**, so nobody doubles up.
- **Resolve when answered.** It reopens if they reply.
- **Be kind and specific.** Link the doc and say what to do next.
- **Keep private things private.** Never ask for passwords or tokens, and
  don't pass on other people's details.
- **Stuck?** Ask in the thread without `?`.

## Where things are

- **Home tab:** the queue, your claimed tickets, your stats and the guide.
- `/support unanswered`: who's waiting longest.
- `/support help`: every command.
