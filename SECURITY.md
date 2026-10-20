# Security policy

## Reporting a vulnerability

**Please don't open a public issue.** Report it privately through GitHub:
the repository's **Security** tab → **Report a vulnerability**. Only the
maintainers can see the report.

Include what's affected, how to reproduce it, and what someone could do
with it. We aim to acknowledge reports within a week and to fix serious
problems before discussing them publicly. We'll credit you unless you'd
rather we didn't.

Only the latest version on `main` is supported.

## What's in scope

For example:

- Anyone who isn't a helper sending replies, closing tickets, or reading
  ticket contents.
- A helper doing admin-only things (settings, roster, macros).
- One user's messages or files reaching another user.
- Leaking tokens or stored data.

Slack's own platform and your hosting provider are out of scope. Report
those to them.

## If you run the bot

- Keep `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` in your host's secret
  settings or `.env`, never in git. If one leaks, regenerate it at
  <https://api.slack.com/apps>.
- Make the support channel **private** and keep the helper roster to the
  people who need it. Helpers can read every ticket.
- `tickets.db` holds message text. Treat backups of it like the messages
  themselves.
- Keep the bot updated. Dependabot keeps its dependencies current in this
  repo.
