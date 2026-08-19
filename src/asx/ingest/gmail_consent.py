"""One-time OAuth consent helper for the Gmail alert mailbox.

Run from a machine with a browser. Produces a refresh token, which is the
only long-lived secret involved and which is scoped to reading mail — it
cannot send, delete, or mark anything read.

    python -m asx.ingest.gmail_consent --client-id ... --client-secret ...

The refresh token is printed once and never stored by this tool: put it in
the environment where `asx detect` runs. Nothing here writes it to disk,
because a secret in a file inside a repository checkout is a secret waiting
to be committed.
"""

from __future__ import annotations

import argparse
import urllib.parse

from asx.ingest.gmail_api import READONLY_SCOPE, TOKEN_URL, _post_form

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
# The out-of-band flow is gone; this is the loopback-less equivalent Google
# still supports for installed apps, where the code is displayed for copying.
REDIRECT = "http://localhost:8765/"


def consent_url(client_id: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": READONLY_SCOPE,
        # offline + consent is what actually returns a refresh token; without
        # them Google issues an access token only and the job dies in an hour.
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange(client_id: str, client_secret: str, code: str) -> dict:
    return _post_form(TOKEN_URL, {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT,
    })


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--code", help="the code from the redirect URL")
    args = ap.parse_args(argv)

    if not args.code:
        print("1. Open this URL, sign in as the ALERT account, and approve:\n")
        print("   " + consent_url(args.client_id) + "\n")
        print("2. The browser will fail to load localhost:8765 — that is")
        print("   expected. Copy the `code=` value out of the address bar.\n")
        print("3. Re-run this command with --code <that value>")
        return

    payload = exchange(args.client_id, args.client_secret, args.code)
    token = payload.get("refresh_token")
    if not token:
        raise SystemExit(
            f"no refresh_token in the response: {payload}. Google only issues "
            f"one with access_type=offline and prompt=consent, and only on the "
            f"first approval — revoke the app's access and try again."
        )
    print("\nSet these on the environment where `asx detect` runs.")
    print("Do not commit them and do not paste them into a chat.\n")
    print(f"  ASX_GMAIL_CLIENT_ID={args.client_id}")
    print("  ASX_GMAIL_CLIENT_SECRET=<the client secret you already have>")
    print(f"  ASX_GMAIL_REFRESH_TOKEN={token}")


if __name__ == "__main__":
    main()
