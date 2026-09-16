"""Forwarding message attachments (images, videos, files) across channels.

Slack file URLs are private and auth'd, so we can't just paste them into
another channel. The bot downloads the bytes with its own token and re-uploads
them into the destination channel/thread via `files.upload` (v2). Skips or
logs anything that fails so a single unreadable file never breaks the relay.
"""

import io
import logging

import requests

MAX_FILE_BYTES = 25 * 1024 * 1024
_DOWNLOAD_CHUNK = 64 * 1024


def _download(file_info: dict, token: str) -> bytes | None:
    url = file_info.get("url_private") or file_info.get("url_private_download")
    if not url:
        return None
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60, stream=True)
        resp.raise_for_status()
    except Exception:
        logging.exception("Couldn't download attachment %s", file_info.get("id"))
        return None
    buf = io.BytesIO()
    seen = 0
    try:
        for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK):
            seen += len(chunk)
            if seen > MAX_FILE_BYTES:
                logging.warning("Skipping attachment %s: larger than %d bytes", file_info.get("id"), MAX_FILE_BYTES)
                return None  # stop reading early instead of buffering the whole oversized file
            buf.write(chunk)
    except Exception:
        logging.exception("Couldn't download attachment %s", file_info.get("id"))
        return None
    return buf.getvalue()


def _message_ts(client, resp, channel: str) -> str | None:
    """The message timestamp Slack created when sharing a just-uploaded file."""
    files = resp.get("files") or ([resp.get("file")] if resp.get("file") else [])
    for f in files:
        for section in (f.get("shares") or {}).values():  # public and private share maps
            for share in section.get(channel, []):
                if share.get("ts"):
                    return share["ts"]
    try:  # fallback: shares omitted from the response — grab the newest message
        hist = client.conversations_history(channel=channel, limit=1)
        if hist.get("messages"):
            return hist["messages"][0]["ts"]
    except Exception:
        logging.exception("Couldn't look up message ts after upload")
    return None


def upload_files(client, channel: str, thread_ts: str | None, files: list | None, token: str) -> list:
    """Re-upload each attachment into a channel/thread; returns the Slack
    message timestamps of the created file messages for reply tracking."""
    if not files:
        return []
    uploaded_ts = []
    for file_info in files:
        data = _download(file_info, token)
        if data is None:
            continue
        name = file_info.get("name") or "attachment"
        try:
            resp = client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=io.BytesIO(data),
                filename=name,
                title=name,
            )
        except Exception:
            logging.exception("Couldn't re-upload attachment %s", file_info.get("id"))
            continue
        ts = _message_ts(client, resp, channel)
        if ts:
            uploaded_ts.append(ts)
    return uploaded_ts
