# Support Bot

> [!NOTE]  
> This project was almost entierly coded by a varity of differant coding agents, such as Claude Code and Open Code, but has been tried and tested by real people.

A Slack help desk that lives in DMs. People message the bot, confirm, and
their question lands in a private support channel as a **ticket**. Helpers
reply in the ticket's thread with a `?` prefix, and the bot relays it back,
signed with the helper's name and role. Nobody needs a new tool, a form, or
a login.

Built for [Hack Club](https://hackclub.com) events (it was first run for
the Bento YSWS), but nothing in it is event-specific. The channel, event
name, roster and canned replies are all set from Slack.

- **Tickets with state:** Claim, Resolve and Reopen buttons. Tickets
  reopen when the person replies, and there's a queue of who's waiting
  longest.
- **Two-way relay:** replies, follow-ups, files and screenshots (with a
  "send this?" check both ways), edits and deletes, all logged in the
  thread.
- **Nudges:** if a ticket waits too long, the bot pings the channel and
  whoever claimed it.
- **Macros:** saved replies with `{name}`-style placeholders.
- **Reach out first:** `?@someone` schedules a message to anyone and opens
  a ticket for the conversation.
- **Home tab:** stats, the unanswered queue, your own claims, built-in
  **helper training**, an admin handbook and setup checklist, and full
  roster/macro/settings management. No config files needed.

## Use cases

- **Event and program support:** hackathons, YSWS programs, game jams.
  Participants DM one bot instead of guessing who to ask.
- **Club or community help desks:** office hours, "ask the organisers",
  onboarding questions.
- **Private reports:** people can raise something with the team without
  posting in a public channel. Only helpers see the support channel.
- **Small-team internal support:** IT, ops or tooling questions, when a
  full ticketing system would be overkill.

**When it's not a fit:** it isn't anonymous (helpers see who asked). It
isn't end-to-end encrypted (messages pass through Slack and the bot's
database). It runs as one process with SQLite, which is fine for
thousands of tickets but not for a 24/7 enterprise help desk with SLAs and
integrations.

## Quick start

1. **Create the Slack app** from [`slack-manifest.yml`](slack-manifest.yml)
   and get two tokens. See [docs/slack-app.md](docs/slack-app.md).
2. **Run it** wherever a Python process can stay up:
   - [Orchard](docs/deploy-orchard.md), Hack Club's hosting
   - [Docker, a VPS, Fly.io, Railway and others](docs/hosting.md)
   - locally:
     ```bash
     cp .env.example .env          # paste in the two tokens
     pip install -r requirements.txt
     python app.py
     ```
3. **Claim admin:** open the bot's **Home** tab in Slack, click **Set up
   the bot** and enter the setup code from the bot's logs. Then follow the
   checklist it shows you.

## Documentation

| | |
|---|---|
| [Slack app setup](docs/slack-app.md) | Manifest, tokens, scopes |
| [Deploy on Orchard](docs/deploy-orchard.md) | Step by step |
| [Other hosting](docs/hosting.md) | Docker, VPS, Fly.io, Railway, Render |
| [Running an event](docs/running-an-event.md) | For admins: setup, helpers, macros, data |
| [Helper cheat sheet](docs/helper-guide.md) | For helpers. The full training is in Slack |
| [How it works](docs/how-it-works.md) | Every behaviour, command and edge case |
| [Contributing](CONTRIBUTING.md) | Dev setup, tests, PR guidelines |
| [Security](SECURITY.md) | Reporting vulnerabilities |

## AI transparency

Most of this codebase was written with AI coding assistants: **Claude
Code** (Anthropic), **OpenCode**, and **GitHub Copilot** (Autofix and its
coding agent). Humans decided what to build, reviewed the changes, tested
them in Slack, and are responsible for what's merged. Commits written with
AI help say so in a `Co-Authored-By` trailer or in the commit message. The
test suite (`pytest`) and CI exist partly so that AI-written changes are
checked by something other than the AI that wrote them.

**The bot itself uses no AI.** It makes no calls to language models or AI
services. Messages only travel between Slack and the server you host it
on, and are stored in that server's SQLite database. See
[Running an event → Data and privacy](docs/running-an-event.md#data-and-privacy).

If you contribute with AI help, that's welcome. Please read
[CONTRIBUTING.md](CONTRIBUTING.md#using-ai-tools) first.

## License

MIT. See [LICENSE](LICENSE).
