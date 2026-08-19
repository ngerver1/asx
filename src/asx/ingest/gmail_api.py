"""Gmail API mailbox source — the only route into a mailbox from a sandboxed
cloud container.

**Why this exists.** IMAP does not work from the cloud execution environment,
and no credential fixes that. Direct TCP is unavailable; everything leaves
through an HTTPS policy proxy. That proxy accepts a CONNECT to
imap.gmail.com:993 and then resets the connection during the TLS handshake,
because IMAP is not HTTPS. Measured, with an HTTPS control through the same
tunnel succeeding:

    gmail.googleapis.com:443  CONNECT 200 -> TLS ok -> HTTP 401
    imap.gmail.com:993        CONNECT 200 -> ConnectionResetError

The Gmail REST API is ordinary HTTPS, so it passes.

**Why this is safer than the IMAP alternative anyway.** An app password grants
full account access and bypasses 2-Step Verification. This uses OAuth scoped
to `gmail.readonly`, which cannot send, delete, modify, or mark anything read
— the "reading your own mail hides it from the platform" failure is not merely
avoided by convention here, it is impossible. The grant is revocable on its
own without touching the account password.

Credentials come from the environment and never from source. On Claude Code
cloud environments, set them as environment variables on the environment
itself so they are injected into each session without ever appearing in a
conversation, a commit, or a log.
"""

from __future__ import annotations

import base64
import email
import email.policy
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message
from typing import Iterable

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class GmailAuthError(RuntimeError):
    """Raised when the stored grant cannot produce an access token."""


@dataclass
class GmailCredentials:
    client_id: str
    client_secret: str
    refresh_token: str

    @classmethod
    def from_env(cls) -> "GmailCredentials":
        missing = [v for v in ("ASX_GMAIL_CLIENT_ID", "ASX_GMAIL_CLIENT_SECRET",
                               "ASX_GMAIL_REFRESH_TOKEN")
                   if not os.environ.get(v)]
        if missing:
            raise GmailAuthError(
                f"missing {', '.join(missing)}. Set them as environment "
                f"variables on the cloud environment (not in the repo, not in "
                f"chat) — see docs/MAILBOX_SETUP.md."
            )
        return cls(os.environ["ASX_GMAIL_CLIENT_ID"],
                   os.environ["ASX_GMAIL_CLIENT_SECRET"],
                   os.environ["ASX_GMAIL_REFRESH_TOKEN"])


def _post_form(url: str, fields: dict, opener=None) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type":
                                          "application/x-www-form-urlencoded"})
    with (opener or urllib.request).urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _get_json(url: str, token: str, opener=None) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with (opener or urllib.request).urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


class GmailAPIMailbox:
    """Reads alert emails through the Gmail REST API.

    Searched by DATE, never by unread state, for the same reason the IMAP
    reader was changed: an alert the owner reads on their phone is no longer
    unread, and an unread-only search would leave a permanent hole in the
    dataset with nothing to alarm on. Re-reading is free because detections
    are idempotent on the ASX announcement number.
    """

    def __init__(self, credentials: GmailCredentials | None = None, *,
                 since_days: int = 7, query: str | None = None,
                 label: str | None = None, max_results: int = 200,
                 opener=None):
        self.credentials = credentials or GmailCredentials.from_env()
        self.since_days = since_days
        self.query = query
        self.label = label or os.environ.get("ASX_GMAIL_LABEL")
        self.max_results = max_results
        self._opener = opener
        self._token: str | None = None

    # -- auth -------------------------------------------------------------
    def access_token(self) -> str:
        if self._token:
            return self._token
        try:
            payload = _post_form(TOKEN_URL, {
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
                "refresh_token": self.credentials.refresh_token,
                "grant_type": "refresh_token",
            }, self._opener)
        except Exception as exc:
            raise GmailAuthError(
                f"could not exchange the refresh token for an access token: "
                f"{type(exc).__name__}: {exc}. If the grant was revoked or the "
                f"consent screen is in testing mode (refresh tokens expire "
                f"after 7 days), re-run the consent step."
            ) from exc
        if "access_token" not in payload:
            raise GmailAuthError(f"token endpoint returned no access_token: {payload}")
        self._token = payload["access_token"]
        return self._token

    # -- search -----------------------------------------------------------
    def search_query(self) -> str:
        if self.query:
            return self.query
        after = (datetime.now(timezone.utc) - timedelta(days=self.since_days))
        parts = [f"after:{after.strftime('%Y/%m/%d')}"]
        if self.label:
            parts.append(f"label:{self.label}")
        return " ".join(parts)

    def _message_ids(self, token: str) -> list[str]:
        ids, page = [], None
        while True:
            params = {"q": self.search_query(),
                      "maxResults": str(min(self.max_results - len(ids), 100))}
            if page:
                params["pageToken"] = page
            payload = _get_json(f"{API_ROOT}/messages?{urllib.parse.urlencode(params)}",
                                token, self._opener)
            ids += [m["id"] for m in payload.get("messages", [])]
            page = payload.get("nextPageToken")
            if not page or len(ids) >= self.max_results:
                return ids[: self.max_results]

    def fetch_new(self) -> Iterable[Message]:
        token = self.access_token()
        for message_id in self._message_ids(token):
            payload = _get_json(f"{API_ROOT}/messages/{message_id}?format=raw",
                                token, self._opener)
            raw = payload.get("raw")
            if not raw:
                continue
            # Gmail returns base64url of the complete RFC822 message, so the
            # same parser handles API, IMAP and saved .eml identically.
            yield email.message_from_bytes(
                base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)),
                policy=email.policy.default)
