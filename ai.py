"""Thin client for Hack Club AI (https://ai.hackclub.com).

The proxy exposes an OpenAI-compatible API at `https://ai.hackclub.com/proxy/v1`
— any chat model in the workspace's allow-list works. We default to a
fast/cheap model (`google/gemini-2.5-flash`) and keep it configurable via
`AI_MODEL`. Authentication is a personal API key from the ai.hackclub.com
dashboard, read from `HACK_CLUB_AI_API_KEY`.

Every call degrades gracefully to `None` if the key is missing, the network
fails, or the response isn't usable — the support flow should never break
just because AI is down.
"""

import json
import logging
import os

import requests

AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://ai.hackclub.com/proxy/v1")
AI_MODEL = os.environ.get("AI_MODEL", "google/gemini-2.5-flash")
AI_MAX_RECENT = int(os.environ.get("AI_MAX_RECENT", "15"))


def api_key() -> str | None:
    return os.environ.get("HACK_CLUB_AI_API_KEY") or None


def chat(messages: list, *, model: str | None = None, max_tokens: int = 1200) -> str | None:
    """One completion call. Returns the model's text or None on any failure."""
    key = api_key()
    if not key:
        logging.warning("HACK_CLUB_AI_API_KEY not set — skipping Hack Club AI call")
        return None
    try:
        resp = requests.post(
            f"{AI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model or AI_MODEL, "messages": messages, "max_tokens": max_tokens},
            timeout=90,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        logging.exception("Hack Club AI request failed (model=%s)", model or AI_MODEL)
        return None


def _extract_json(raw: str) -> dict | None:
    """Best-effort parse of a model response that may include code fences or prose."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def summarize_conversation(user_label: str, question: str, answers: list) -> str | None:
    """One-paragraph summary of a resolved ticket's Q&A for the support canvas."""
    if not answers:
        answers_text = "(no written reply recorded — ticket may have been closed early)"
    else:
        answers_text = "\n".join(f"- {a}" for a in answers)
    prompt = (
        f"You log resolved helpdesk tickets for the Hack Club event support team.\n"
        f"Summarise the exchange below into 1-3 tight sentences a future helper "
        f"could skim to instantly understand what was asked and how it was resolved. "
        f"Do not address the user; write in past tense, impersonally.\n\n"
        f"User: {user_label}\n"
        f"Question: {question}\n"
        f"Answers: {answers_text}"
    )
    text = chat([{"role": "user", "content": prompt}], max_tokens=400)
    if not text:
        return None
    return text.replace("\n", " ").strip()


def find_similar(question: str, past: list) -> dict | None:
    """Check a new question against previously resolved tickets.

    `past` is a list of {"question", "summary"} dicts (newest first). Returns
    None when nothing similar is found or the AI can't be reached; otherwise
    {"matched_question": str, "suggested_reply": str} ready to be offered to a
    helper as a one-click reply.
    """
    if not past:
        return None
    past_block = "\n\n".join(
        f"Ticket {i + 1}:\n  Asked: {p['question']}\n  Resolved: {p['summary']}" for i, p in enumerate(past)
    )
    prompt = (
        f"You help the Hack Club event support team answer repeated questions.\n"
        f"Below are summaries of previously resolved tickets, most recent last. "
        f"A new user just asked: {question!r}\n\n"
        f"{past_block}\n\n"
        f"Is the new question substantially similar in meaning to any of the resolved "
        f"tickets ABOVE? If yes, reply with ONLY a JSON object:\n"
        f'{{"similar": true, "matched_question": "...", "suggested_reply": "..."}}\n'
        f"where matched_question is a short restatement of the similar past question and "
        f"suggested_reply is a friendly, support-appropriate answer to the NEW question "
        f"using the resolution of the matched ticket (written to the user, no signature).\n"
        f'If none is similar, reply with ONLY {{"similar": false}}.'
    )
    raw = chat([{"role": "user", "content": prompt}], max_tokens=600)
    data = _extract_json(raw) if raw else None
    if not data or not data.get("similar"):
        return None
    return {
        "matched_question": str(data.get("matched_question", "")).strip(),
        "suggested_reply": str(data.get("suggested_reply", "")).strip(),
    }
