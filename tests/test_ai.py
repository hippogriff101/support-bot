import ai


class FakeResponse:
    def __init__(self, content):
        self._json = {"choices": [{"message": {"content": content}}]}

    def raise_for_status(self):
        return None

    def json(self):
        return self._json


def test_extract_json_plain(monkeypatch):
    assert ai._extract_json('{"similar": true, "x": 1}') == {"similar": True, "x": 1}


def test_extract_json_fenced():
    raw = '```json\n{"similar": false}\n```'
    assert ai._extract_json(raw) == {"similar": False}


def test_extract_json_inside_prose():
    raw = 'Sure! Here you go:\n{"similar": true, "matched_question": "a", "suggested_reply": "b"}\nHope that helps.'
    assert ai._extract_json(raw) == {
        "similar": True,
        "matched_question": "a",
        "suggested_reply": "b",
    }


def test_extract_json_garbage():
    assert ai._extract_json("not json at all") is None


def test_chat_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("HACK_CLUB_AI_API_KEY", raising=False)
    assert ai.chat([{"role": "user", "content": "hi"}]) is None


def test_summarize_conversation(monkeypatch):
    monkeypatch.setenv("HACK_CLUB_AI_API_KEY", "test-key")
    monkeypatch.setattr(
        ai.requests,
        "post",
        lambda *a, **k: FakeResponse("User was stuck on login; helper fixed it."),
    )
    out = ai.summarize_conversation("Kai", "i can't log in", ["reset your password"])
    assert out == "User was stuck on login; helper fixed it."


def test_find_similar_matches(monkeypatch):
    monkeypatch.setenv("HACK_CLUB_AI_API_KEY", "test-key")
    monkeypatch.setattr(
        ai.requests,
        "post",
        lambda *a, **k: FakeResponse(
            '{"similar": true, "matched_question": "login issues", "suggested_reply": "Try resetting your password."}'
        ),
    )
    past = [{"question": "can't log in", "summary": "Reset password fixed it"}]
    out = ai.find_similar("my login is broken", past)
    assert out == {
        "matched_question": "login issues",
        "suggested_reply": "Try resetting your password.",
    }


def test_find_similar_no_match(monkeypatch):
    monkeypatch.setenv("HACK_CLUB_AI_API_KEY", "test-key")
    monkeypatch.setattr(ai.requests, "post", lambda *a, **k: FakeResponse('{"similar": false}'))
    past = [{"question": "can't log in", "summary": "Reset password fixed it"}]
    assert ai.find_similar("where's the bathroom", past) is None


def test_find_similar_empty_history(monkeypatch):
    assert ai.find_similar("anything", []) is None
