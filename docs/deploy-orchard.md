# Deploy on Orchard

Orchard is Hack Club's hosting platform.
It builds a GitHub repo with its Dockerfile and keeps the container
running, which is all this bot needs.

**Before you start:** [create the Slack app](slack-app.md) and have the two
tokens ready (`xoxb-…` and `xapp-…`).

## 1. Get the code into GitHub

Fork this repo, or push your copy to a GitHub account or organisation
you control. Orchard deploys from GitHub. Make sure the **Orchard GitHub
App** is installed on that account or org, with access to the repo, and
that your Orchard account is linked to GitHub.

## 2. Create the deployment

In Orchard, open (or create) a **project** and add a **deployment from
GitHub**:

| Setting | Value |
|---|---|
| Repository / branch | your fork, `main` |
| Build | **Dockerfile** at the repo root (Orchard finds it) |
| Port | `8080`, the health check (see below) |
| Replicas | **1**. Never more (see the note below) |

## 3. Environment variables

Add these as environment variables (secrets) on the deployment:

| Key | Value |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-…` |
| `SLACK_APP_TOKEN` | `xapp-…` |
| `EVENT_NAME` | optional, e.g. `Bento`. You can also set it from Slack later |

Don't set `DB_PATH`, `HELPERS_FILE` or `MACROS_FILE`. The Dockerfile
already points them at `/data`.

## 4. Add a persistent volume

This step matters. Without it, every redeploy wipes your tickets and helper
roster.

Add a **persistent volume** (Orchard calls it a PVC) to the deployment:

| Setting | Value |
|---|---|
| Mount path | `/data` |
| Size | `1Gi` is plenty (thousands of tickets) |

## 5. Deploy and claim admin

1. Deploy, and open the deployment's **logs**. Once it's connected you'll
   see a line like:
   ```
   WARNING:root:No admins yet — open the bot's Home tab in Slack, click 'Set up the bot' and enter setup code: 1a2b3c4d
   ```
2. In Slack, open the bot (search its name) → **Home** tab → **Set up the
   bot**, and enter the code.
3. You land in **Settings**. Pick your support channel (invite the bot to
   it first). The Home tab's **setup checklist** walks you through the
   rest.

You don't need a domain or ingress. Socket Mode means the bot connects out
to Slack; nothing connects in.

## Health checks and the port

With `PORT` set (the Dockerfile sets `8080`), the bot answers `GET /` with
`ok`. If Orchard asks for a health check, use an **HTTP check on `/`, port
`8080`**. It only shows the process is alive. To check that it's actually
connected to Slack, open its Home tab or DM it.

## Only ever one copy

Run **exactly one replica**, and if Orchard lets you choose an update
strategy, pick **Recreate** rather than rolling updates. Two copies running
at once would both receive every Slack event and answer every message
twice. The volume can also only be attached to one container at a time,
so a rolling update can get stuck waiting for it.

## Updating

Push to your branch and redeploy, or turn on Orchard's **auto-deploy** for
the branch. The database upgrades itself on startup. Watch the logs for
"deployed successfully" and the bot's message in the support channel.

## Backups

Everything lives in `/data`: `tickets.db`, `helpers.json` and
`macros.json`. To keep a copy (say, at the end of an event), use Orchard's
shell/exec on the running container to copy those files out. Or have an
admin export what they need first. See
[Running an event → Data and privacy](running-an-event.md#data-and-privacy).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Crashes on start with `KeyError: 'SLACK_BOT_TOKEN'` | Env var missing or misspelled. |
| `invalid_auth` / `not_authed` in logs | Wrong token, or the app was reinstalled and the token changed. |
| Bot online but DMs do nothing | The **Messages tab** isn't enabled in the app (use the manifest), or the `message.im` event is missing. |
| "Support isn't quite set up yet" reply | No support channel picked yet. Home tab → Settings. |
| Helpers' `?` replies are ignored | The bot isn't in the support channel (`/invite` it), or they aren't on the roster. |
| Roster or tickets gone after a redeploy | No volume at `/data`. See step 4. |
| Every message answered twice | Two replicas, or an old deployment still running. |
