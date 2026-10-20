"""Behaviour tests for the minimal Linear GraphQL client (fetch issue by id)."""

from argus.jarvis import linear


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_issue_parses_node(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "tok")
    monkeypatch.setattr(linear.httpx, "post", lambda *a, **k: _Resp(
        {"data": {"issues": {"nodes": [
            {"identifier": "ENG-449", "title": "Fan-in", "description": "story"}]}}}))
    assert linear.fetch_issue("ENG-449") == {
        "identifier": "ENG-449", "title": "Fan-in", "description": "story"}


def test_fetch_issue_none_without_token(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    assert linear.fetch_issue("ENG-1") is None


def test_fetch_issue_none_on_unknown_id(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "tok")
    monkeypatch.setattr(linear.httpx, "post",
                        lambda *a, **k: _Resp({"data": {"issues": {"nodes": []}}}))
    assert linear.fetch_issue("ENG-999") is None


def test_fetch_issue_none_on_network_error(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "tok")

    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(linear.httpx, "post", _boom)
    assert linear.fetch_issue("ENG-1") is None


def test_fetch_issue_rejects_malformed_identifier(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "tok")
    assert linear.fetch_issue("not-an-id") is None
    assert linear.fetch_issue("ENG-") is None
