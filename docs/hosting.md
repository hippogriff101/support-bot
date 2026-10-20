# Hosting elsewhere

The bot runs anywhere a process can stay up. On Orchard, see
[deploy-orchard.md](deploy-orchard.md). Whatever you use, it needs:

- **One long-running process.** It connects out to Slack over Socket Mode,
  so you need **no public URL, domain, HTTPS or webhook**. Serverless and
  "sleep when idle" plans don't work: the bot must be awake to receive
  messages.
- **Exactly one copy.** Two running at once would answer every message
  twice. Turn off autoscaling and use "recreate", not rolling updates.
- **Persistent storage** for `tickets.db` and `helpers.json`. With Docker
  that's a volume at `/data`. Without it, a redeploy forgets every ticket
  and your helper roster.
- **Two secrets:** `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`.
- Python 3.12 (or the Dockerfile). It's light, so the smallest instance
  size is fine.

If your host insists on a web port or a health check, set `PORT` (the
Dockerfile uses `8080`) and point the check at `GET /`.

## Docker (any server)

```bash
docker build -t support-bot .
docker run -d --name support-bot --restart unless-stopped \
  --env-file .env \
  -v support-bot-data:/data \
  support-bot
docker logs -f support-bot     # the setup code is printed here
```

Or with Compose:

```yaml
services:
  support-bot:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - data:/data
volumes:
  data:
```

## A VPS without Docker (systemd)

```bash
git clone https://github.com/<you>/support-bot.git /opt/support-bot
cd /opt/support-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env   # tokens; DB_PATH etc. can stay default
```

`/etc/systemd/system/support-bot.service`:

```ini
[Unit]
Description=Slack support bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/support-bot
ExecStart=/opt/support-bot/.venv/bin/python app.py
Restart=always
RestartSec=5
User=support-bot

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd --system support-bot && sudo chown -R support-bot /opt/support-bot
sudo systemctl enable --now support-bot
journalctl -u support-bot -f        # setup code
```

The data files live in `/opt/support-bot`, so a `git pull` and restart
keeps them.

## Fly.io

```bash
fly launch --no-deploy            # uses the Dockerfile; say no to a database
fly volumes create data --size 1
fly secrets set SLACK_BOT_TOKEN=xoxb-… SLACK_APP_TOKEN=xapp-…
```

In `fly.toml`, mount the volume and keep it to one machine that never
sleeps:

```toml
[mounts]
  source = "data"
  destination = "/data"

[http_service]
  internal_port = 8080
  auto_stop_machines = "off"
  min_machines_running = 1
```

Then `fly deploy` and `fly scale count 1`.

## Railway, Render and similar

- **Railway:** deploy the repo (it picks up the Dockerfile), add the two
  variables, and attach a **volume** mounted at `/data`. Keep it at one
  replica.
- **Render:** create a **Background Worker** from the repo (Docker), add
  the two variables and a **persistent disk** at `/data`. Background
  workers don't need a port. Disks need a paid instance.
- **Anything else:** use a container or "worker" service type with a
  persistent volume at `/data`, one instance, and no sleeping.

## Running it on your own computer

Fine for trying it out, but the bot is offline whenever your computer
sleeps.

```bash
cp .env.example .env
pip install -r requirements.txt
python app.py
```
