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


def _download(file_info: dict, token: str) -> bytes | None:
    url = file_info.get("url_private") or file_info.get("url_private_download")
    if not url:
        return None
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        resp.raise_for_status()
    except Exception:
        logging.exception("Couldn't download attachment %s", file_info.get("id"))
        return None
    if len(resp.content) > MAX_FILE_BYTES:
        logging.warning("Skipping attachment %s: larger than %d bytes", file_info.get("id"), MAX_FILE_BYTES)
        return None
    return resp.content


def upload_files(client, channel: str, thread_ts: str | None, files: list | None, token: str) -> list:
    """Re-upload each attachment into a channel/thread. Returns the uploaded responses."""
    if not files:
        return []
    uploaded = []
    for file_info in files:
        data = _download(file_info, token)
        if data is None:
            continue
        name = file_info.get("name") or "attachment"
        try:
            resp = client.files_upload_v2(
                channel_id=channel,
                thread_ts=thread_ts,
                file=io.BytesIO(data),
                filename=name,
                title=name,
                alt_text=file_info.get("alt_text") or f"Attachment: {name}",
            )
            uploaded.append(resp)
        except Exception:
            logging.exception("Couldn't re-upload attachment %s", file_info.get("id"))
    return uploaded
