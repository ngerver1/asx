"""Gmail API mailbox source.

Exercised end to end against a stub transport: the point is that a message
arriving over the API produces exactly the same Detection as the same message
read from a saved .eml, so calibration done against fixtures holds in
production.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest

from asx.ingest.gmail_api import GmailAPIMailbox, GmailAuthError, GmailCredentials
from asx.ingest.mailbox import EmlDirectory, detection_from_email

FIXTURES = Path(__file__).parent.parent / "fixtures" / "mailbox"
CREDS = GmailCredentials("cid", "secret", "refresh")


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class StubTransport:
    """Stands in for urllib, recording every request."""

    def __init__(self, messages: dict[str, bytes], token="tok-123"):
        self.messages, self.token, self.requests = messages, token, []

    def urlopen(self, req, timeout=None):
        url = req.full_url
        self.requests.append((url, dict(req.headers), req.data))
        if url.startswith("https://oauth2.googleapis.com/token"):
            return _Response(json.dumps({"access_token": self.token,
                                         "expires_in": 3599}).encode())
        assert req.headers.get("Authorization") == f"Bearer {self.token}", req.headers
        if "/messages?" in url:
            return _Response(json.dumps(
                {"messages": [{"id": k} for k in self.messages]}).encode())
        msg_id = url.split("/messages/")[1].split("?")[0]
        raw = base64.urlsafe_b64encode(self.messages[msg_id]).decode().rstrip("=")
        return _Response(json.dumps({"raw": raw}).encode())


def _fixture_bytes():
    return {p.stem: p.read_bytes() for p in sorted(FIXTURES.glob("*.eml"))}


def test_api_messages_parse_identically_to_saved_eml():
    """The calibration set was built from .eml files. If the API path decoded
    differently, every gold expectation would be measuring the wrong thing."""
    stub = StubTransport(_fixture_bytes())
    api = {d.key(): d for d in
           (detection_from_email(m) for m in
            GmailAPIMailbox(CREDS, opener=stub).fetch_new())}
    disk = {d.key(): d for d in
            (detection_from_email(m) for m in EmlDirectory(FIXTURES).fetch_new())}

    assert set(api) == set(disk)
    assert len(api) == len(list(FIXTURES.glob("*.eml")))
    for key, d in api.items():
        other = disk[key]
        assert (d.ticker, d.title, d.lodged_at, d.price_sensitive,
                d.manual_open_urls, d.key()) == (
            other.ticker, other.title, other.lodged_at, other.price_sensitive,
            other.manual_open_urls, other.key())


def test_search_is_by_date_never_by_unread():
    """Searching unread would let reading an alert on a phone punch a
    permanent hole in the dataset."""
    stub = StubTransport(_fixture_bytes())
    list(GmailAPIMailbox(CREDS, since_days=3, opener=stub).fetch_new())
    listing = next(u for u, _h, _d in stub.requests if "/messages?" in u)
    assert "after%3A" in listing
    assert "unread" not in listing.lower() and "is%3A" not in listing


def test_a_label_scopes_the_search():
    stub = StubTransport(_fixture_bytes())
    list(GmailAPIMailbox(CREDS, label="asx-alerts", opener=stub).fetch_new())
    listing = next(u for u, _h, _d in stub.requests if "/messages?" in u)
    assert "label%3Aasx-alerts" in listing


def test_nothing_is_ever_modified():
    """Read-only scope makes this structural, but the client must not even
    attempt a mutation: every API call is a GET, and the only POST is the
    token exchange."""
    stub = StubTransport(_fixture_bytes())
    list(GmailAPIMailbox(CREDS, opener=stub).fetch_new())
    for url, _headers, data in stub.requests:
        if url.startswith("https://oauth2.googleapis.com/token"):
            continue
        assert data is None, f"non-token request carried a body: {url}"
        assert "/modify" not in url and "/trash" not in url and "/send" not in url


def test_missing_credentials_say_what_to_set(monkeypatch):
    for var in ("ASX_GMAIL_CLIENT_ID", "ASX_GMAIL_CLIENT_SECRET",
                "ASX_GMAIL_REFRESH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(GmailAuthError) as excinfo:
        GmailCredentials.from_env()
    assert "ASX_GMAIL_REFRESH_TOKEN" in str(excinfo.value)
    assert "not in chat" in str(excinfo.value)


def test_a_revoked_grant_fails_loudly_with_the_remedy():
    class Broken(StubTransport):
        def urlopen(self, req, timeout=None):
            raise OSError("HTTP Error 400: invalid_grant")

    with pytest.raises(GmailAuthError) as excinfo:
        GmailAPIMailbox(CREDS, opener=Broken({})).fetch_new().__next__()
    assert "refresh token" in str(excinfo.value)
    assert "testing mode" in str(excinfo.value)
