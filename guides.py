"""The in-Slack guides: helper training, the admin handbook, and the "how to
get help" steps people see on the Home tab.

Plain data, so the wording can change without touching any Block Kit code —
home.py turns each page into a modal, with Back/Next buttons between pages.
Every page is one mrkdwn section, so keep each under Slack's 3000-character
section limit (a test checks).
"""

# (title, body) pairs. Titles show in bold at the top of each page.
HELPER_GUIDE = [
    (
        ":wave: Welcome to the support team",
        "This bot turns DMs into tickets so nobody's question gets lost.\n\n"
        "*How a ticket moves:*\n"
        "1. Someone DMs the bot and confirms they want it sent to support.\n"
        "2. It lands in the support channel as a *ticket* — one message, with a thread.\n"
        "3. You reply *in that thread*. Anything starting with `?` goes back to them.\n"
        "4. Their replies come back into the same thread. Resolve it when you're done.\n\n"
        "The next few pages cover everything you need — about two minutes. "
        "You can reopen this any time from the bot's *Home* tab or with `/support guide`.",
    ),
    (
        ":speech_balloon: Replying",
        "• `?your message` in a ticket's thread → sent to the person, signed with your name and role.\n"
        "• No `?` → *internal*. Only the support team sees it — use it to discuss, tag a teammate, or take notes.\n"
        "• `?macro-name` → sends a saved reply. `/support macros` lists them all.\n"
        "• `{name}` in a reply becomes their first name: `?Hi {name}, thanks for waiting!`\n\n"
        ":bulb: Always reply in the ticket's *thread*, not the channel. A `?` anywhere else isn't sent to "
        "anyone, and the bot will tell you so.",
    ),
    (
        ":label: Claiming and resolving",
        "Each ticket has buttons that follow its state:\n"
        "• *Claim* — \"I've got this one.\" Others can still help; it's just so you don't double up.\n"
        "• *Resolve* — done. Adds a tick and moves it out of the queue. The person isn't messaged.\n"
        "• *Reopen* — if it needs another look.\n\n"
        "`?claim`, `?unclaim` and `?close` do the same from the thread.\n\n"
        "A resolved ticket *reopens by itself* if the person replies, so it's safe to resolve early. "
        "If a ticket waits too long without a reply, the bot nudges the channel (and whoever claimed it).",
    ),
    (
        ":paperclip: Files and screenshots",
        "• `?` *with* a file attached → the file goes to the person with your message.\n"
        "• A file *without* `?` → the bot privately asks whether to send it, so a screenshot meant for the "
        "team never reaches them by accident.\n"
        "• When someone sends you a file, they're asked to confirm first too, then it appears in the thread.\n\n"
        "If a file can't be copied (too large, deleted), there'll be a note in the thread.",
    ),
    (
        ":pencil2: Fixing mistakes",
        "• *Edit* your `?` message and their copy updates too.\n"
        "• *Delete* it, or edit the `?` away, and their copy is removed.\n"
        "• The *Edit reply* / *Delete reply* message shortcuts (the `⋯` menu) do the same.\n\n"
        "Every edit and delete is logged in the thread with timestamps, so there's always a record of what "
        "was actually sent. Times are UTC.",
    ),
    (
        ":outbox_tray: Messaging someone first",
        "Need to reach someone who hasn't messaged the bot? Type `?@their-name` — in a ticket's thread, or "
        "on its own in the support channel.\n\n"
        "You'll get a *Continue* prompt, then a form for the first message and when to send it (now, or up "
        "to 48 hours later). The conversation gets its own ticket, already claimed by you — reply there with "
        "`?…` like any other.",
    ),
    (
        ":star2: Good support habits",
        "• *Acknowledge quickly.* `?Looking into this now!` beats ten minutes of silence.\n"
        "• *Claim before a deep dive*, so two people don't research the same thing.\n"
        "• *Resolve when it's answered* — it reopens if they come back.\n"
        "• *Be kind and specific.* Link to docs; say what to do next.\n"
        "• *Keep private things private.* Don't send someone else's details, and never ask for passwords.\n"
        "• *Not sure? Ask in the thread without `?`.* Teammates and admins will see it.\n\n"
        "That's it! Hit *Finish training* and you're ready.",
    ),
]

ADMIN_GUIDE = [
    (
        ":rocket: First-time setup",
        "1. *Settings* → pick the *support channel* (a private channel your helpers are in). "
        "Invite the bot to it first with `/invite`.\n"
        "2. Set the *event name* — it appears in signatures and messages.\n"
        "3. Choose when to *nudge* about unanswered tickets (0 turns it off).\n"
        "4. *Add helpers*, then ask them to take the training on the Home tab.\n"
        "5. Look over the starter *macros* and make them yours.\n\n"
        "The checklist at the top of your Home tab ticks itself off as you go.",
    ),
    (
        ":busts_in_silhouette: Helpers and admins",
        "• *Add helper* on the Home tab, or `/support promote @someone Role`.\n"
        "• The *role* is shown in their reply signature, e.g. `— @Sam, Clubs Team`.\n"
        "• Tick *Admin* for people who should manage settings, helpers and macros.\n"
        "• `/support demote @someone` removes them. The bot won't let you remove the last admin.\n\n"
        "The roster shows who's finished the helper training.",
    ),
    (
        ":memo: Macros",
        "Macros are saved replies: `?refund-policy` sends the macro's text.\n\n"
        "• Add or edit them on the Home tab, or `/support macros <name> <text>`.\n"
        "• Names can't contain spaces — use dashes.\n"
        "• `{name}`, `{event}` and `{date}` are filled in when sent.\n\n"
        "Good macros answer the questions you get every day, and end with what to do next.",
    ),
    (
        ":chart_with_upwards_trend: During the event",
        "• *Waiting on a reply* on the Home tab lists the longest-waiting tickets first. `/support unanswered` "
        "shows the same.\n"
        "• The pinned *Support stats* message in the channel tracks open vs resolved.\n"
        "• Nudges post in the ticket thread *and* the channel, pinging whoever claimed it.\n"
        "• Changing the support channel mid-event moves the stats, but open tickets stay in the old "
        "channel — finish those first.",
    ),
    (
        ":lock: Data and privacy",
        "Tickets, relayed messages and the edit/delete log are stored in the bot's SQLite database "
        "(`tickets.db`); the roster is in `helpers.json`. Both live wherever the bot is hosted.\n\n"
        "• Back them up if you need a record — they're plain files.\n"
        "• Tell people that messages to the bot are read by the support team.\n"
        "• When the event's over, delete what you don't need to keep.\n\n"
        "The bot doesn't send anything to AI services or any third party — only to Slack.",
    ),
]

# What someone who isn't a helper sees on the Home tab.
HOW_TO_GET_HELP = (
    "*How to get help*\n"
    "1. Send me a DM (the *Messages* tab above) describing what you need.\n"
    "2. I'll ask whether to send it to the support team — click *Yes, send it*.\n"
    "3. A helper replies right there in the thread. Reply in that same thread to keep the conversation going.\n\n"
    "Screenshots help! I'll check with you before sending any file."
)

GUIDES = {"helper": HELPER_GUIDE, "admin": ADMIN_GUIDE}
TITLES = {"helper": "Helper training", "admin": "Admin handbook"}
