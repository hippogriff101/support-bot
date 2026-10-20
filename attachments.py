"""Copy files between a user's DM thread and their ticket's support thread.

Slack doesn't let a bot "forward" a file into another conversation, so each
file is downloaded with the bot token (needs `files:read`) and re-uploaded
into the destination thread (needs `files:write`). Uploads post as the bot
itself — Slack doesn't support a custom name/photo on file uploads — so the
accompanying text message is what says who sent them.
"""

import logging
import urllib.request

# Slack's own upload limit is far higher, but each file is buffered in
# memory, so skip anything unreasonable rather than risk the process.
MAX_BYTES = 100 * 1024 * 1024


def _download(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read(MAX_BYTES + 1)


def relayable(files: list | None) -> list:
    """Files from a message event that can actually be copied — drops
    tombstones and files hidden by the workspace's plan limits."""
    return [f for f in files or [] if f.get("url_private_download") and f.get("mode") != "tombstone"]


def summary(files: list | None) -> list:
    """The three fields we need to describe a file in a prompt and fetch it
    later, as plain strings.

    Full Slack file objects are far too big to park in a button value when a
    prompt has to after a restart (they carry `thumb_*`, `permalink` and more),
    so prompts stash this instead and hand it straight back to relay_files().
    """
    return [
        {
            "id": f.get("id", ""),
            "name": f.get("name") or f.get("title") or "attachment",
            "url": f["url_private_download"],
        }
        for f in files or []
    ]


def describe(files: list | None) -> str:
    """`<url|a.png>` lines for a confirm prompt — the file's name, linked to
    the message it came from so the sender can check it's the right one."""
    lines = []
    for f in files or []:
        name = f.get("name") or f.get("title") or "attachment"
        url = f.get("url") or f.get("permalink") or f.get("url_private")
        lines.append(f"<{url}|{name}>" if url else f"`{name}`")
    return "\n".join(lines)


def relay_files(client, files: list, channel: str, thread_ts: str) -> list[str]:
    """Copy `files` into `channel`'s `thread_ts` thread. Returns the names of
    any that couldn't be copied, so the caller can say so instead of the
    file silently going missing."""
    uploads, failed = [], []
    for f in files or []:
        name = f.get("name") or f.get("title") or "attachment"
        url = f.get("url") or f.get("url_private_download")
        if not url:
            continue
        try:
            content = _download(url, client.token)
            if len(content) > MAX_BYTES:
                raise ValueError("file too large")
            uploads.append({"content": content, "filename": name, "title": f.get("title") or name})
        except Exception as e:
            logging.warning("Couldn't download attachment %s: %s", name, e)
            failed.append(name)

    if uploads:
        try:
            client.files_upload_v2(file_uploads=uploads, channel=channel, thread_ts=thread_ts)
        except Exception as e:
            logging.warning("Couldn't upload %d attachment(s): %s", len(uploads), e)
            failed.extend(u["filename"] for u in uploads)
    return failed
