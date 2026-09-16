import media

FILES = [
    {
        "id": "F1",
        "name": "one.png",
        "url_private": "https://files.slack.com/files/one",
        "alt_text": "alt one",
    },
    {
        "id": "F2",
        "name": "two.png",
        "url_private": "https://files.slack.com/files/two",
        "alt_text": "alt two",
    },
]


class FakeClient:
    def __init__(self, shares=None):
        self._shares = shares or [{"ts": "1111.1"}, {"ts": "1111.2"}]
        self._i = 0
        self.uploads = []
        self.history_calls = []

    def files_upload_v2(self, **kwargs):
        self.uploads.append(kwargs)
        ts = self._shares[self._i]
        self._i += 1
        file = {"id": f"FU{self._i}", "shares": {"private": {"C1": [ts]}}}
        return {"ok": True, "files": [file]}

    def conversations_history(self, **kwargs):
        self.history_calls.append(kwargs)
        return {"messages": [{"ts": "2222.2"}]}


def test_upload_files_returns_ts_list(monkeypatch):
    monkeypatch.setattr(media, "_download", lambda info, token: b"data")
    client = FakeClient()
    result = media.upload_files(client, "C1", "thread1", FILES, "token")
    assert result == ["1111.1", "1111.2"]
    assert [u["channel"] for u in client.uploads] == ["C1", "C1"]  # correct param, not channel_id
    assert [u["thread_ts"] for u in client.uploads] == ["thread1", "thread1"]
    assert [u["filename"] for u in client.uploads] == ["one.png", "two.png"]


def test_upload_files_skips_failed_download(monkeypatch):
    monkeypatch.setattr(media, "_download", lambda info, token: None)
    client = FakeClient()
    assert media.upload_files(client, "C1", None, FILES, "token") == []
    assert client.uploads == []


def test_upload_files_falls_back_to_history(monkeypatch):
    monkeypatch.setattr(media, "_download", lambda info, token: b"data")
    client = FakeClient(shares=[{}, {}, None])
    result = media.upload_files(client, "C1", None, FILES, "token")
    assert result == ["2222.2", "2222.2"]
    assert len(client.history_calls) == 2


class FakeResp:
    def __init__(self, chunks):
        self._chunks = chunks

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        for c in self._chunks:
            yield c


def _fake_get(chunks):
    def get(url, headers, timeout, stream):
        assert stream
        return FakeResp(chunks)

    return get


def test_download_streams_and_stops_at_cap(monkeypatch):
    monkeypatch.setattr(media.requests, "get", _fake_get([b"a" * 1024, b"b" * 1024]))
    assert media._download(FILES[0], "token") == b"a" * 1024 + b"b" * 1024


def test_download_returns_none_over_cap(monkeypatch):
    big = b"x" * (media.MAX_FILE_BYTES + 1)
    monkeypatch.setattr(media.requests, "get", _fake_get([big, b"more"]))
    assert media._download(FILES[0], "token") is None


def test_download_none_without_private_url(monkeypatch):
    called = []

    def fail_get(url, headers, timeout, stream):
        called.append(url)
        raise AssertionError("should not hit network")

    monkeypatch.setattr(media.requests, "get", fail_get)
    file = {"id": "F9", "name": "x.png"}  # no url_private / url_private_download
    assert media._download(file, "token") is None
    assert called == []


def test_message_ts_uses_first_ts(monkeypatch):
    resp = {"ok": True, "files": [{"shares": {"public": {"C2": [{"ts": "9.9"}]}, "private": {"C1": [{"ts": "1.1"}]}}}]}
    client = FakeClient()
    assert media._message_ts(client, resp, "C1") == "1.1"
