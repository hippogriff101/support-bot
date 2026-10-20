# Slack app setup

You need a Slack app and two tokens: a **bot token** (`xoxb-…`) and an
**app-level token** (`xapp-…`). You'll need permission to install apps to
your workspace. If you don't have it, ask a workspace admin to do the
install step.

## The quick way: from the manifest

1. Go to <https://api.slack.com/apps> → **Create New App** → **From an app
   manifest**, and pick your workspace.
2. Paste in the contents of [`slack-manifest.yml`](../slack-manifest.yml).
   Change `name`, `description` and `display_name` if you like. Leave the
   rest as is.
3. **Create**, then **Install to Workspace** and allow it.
4. **OAuth & Permissions** → copy the **Bot User OAuth Token** (`xoxb-…`).
   That's `SLACK_BOT_TOKEN`.
5. **Basic Information** → **App-Level Tokens** → **Generate Token and
   Scopes**. Name it anything, add the `connections:write` scope, generate,
   and copy it (`xapp-…`). That's `SLACK_APP_TOKEN`.
6. Optional: under **Basic Information → Display Information**, upload an
   icon. It's what people see when they DM the bot.

Then make a **private channel** for your helpers (the support channel) and
`/invite @YourBot` to it. You'll pick it from the bot's Home tab once it's
running.

> **Keep the tokens secret.** Anyone with them can read and post as your
> bot. Put them in `.env` or your host's secret settings, never in git. If
> one leaks, regenerate it on the same pages.

## What the manifest sets up, and why

| Part | Why the bot needs it |
|---|---|
| **Socket Mode** | The bot connects out to Slack, so it needs no public URL, domain or HTTPS certificate. |
| **Home tab** | Setup, stats, the helper training and the admin dashboard all live there. |
| **Messages tab** (writable) | So people can DM the bot. |
| `/support` command | Help, guides and admin commands. |
| **Edit reply** / **Delete reply** shortcuts | Fix or unsend a reply from the message's `⋯` menu. |
| `chat:write`, `chat:write.customize` | Post, and show the helper's or user's own name and photo on relayed messages. |
| `im:history`, `im:read`, `im:write` | Read DMs to the bot, and open DMs for `?@someone`. |
| `groups:history` / `channels:history` | Read replies in a private / public support channel. |
| `files:read`, `files:write` | Copy attachments between the DM and the support thread. |
| `reactions:write` | 👀, ✅ and the resolved reaction. |
| `users:read` | Look up names and photos. |
| Events: `message.im`, `message.groups`, `message.channels`, `app_home_opened` | Hear DMs, support-channel replies and Home tab opens. |

If you only ever use a private support channel, you can remove
`channels:history` and `message.channels`, and the other way round for a
public one. Keeping both is harmless.

## Changing scopes later

If you add a scope to an app that's already installed, Slack asks you to
**reinstall** it before the new permission works. The bot token usually
stays the same, but check it on **OAuth & Permissions** afterwards.
