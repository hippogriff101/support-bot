# The bot is one long-running Socket Mode process: it connects out to Slack,
# so it needs no public URL. Everything it writes lives in /data — mount a
# persistent volume there or tickets and the helper roster reset on redeploy.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/tickets.db \
    HELPERS_FILE=/data/helpers.json \
    MACROS_FILE=/data/macros.json \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py macros.json ./
RUN mkdir -p /data

# Only a health check listens here (GET / -> "ok"), for hosts that expect a
# port. Slack traffic never comes in this way.
EXPOSE 8080
CMD ["python", "app.py"]
