"""One-time OAuth consent helper for the investorpa.com MCP endpoint.

Run from a machine with a browser, signed in to the InvestorPA account:

    python -m asx.ingest.investorpa_consent                  # step 1: register + URL
    python -m asx.ingest.investorpa_consent --code ... \\
        --client-id ... --verifier ...                       # step 2: exchange

Produces a refresh token, printed once and never written to disk — a secret in
a file inside a repository checkout is a secret waiting to be committed. Put it
in the environment where `asx detect --source investorpa` runs.

Three things about this server make the flow simpler than the Gmail one, and
all three come from its published metadata rather than from memory
(https://investorpa.com/.well-known/oauth-authorization-server, read
20 Aug 2026):

  * **Dynamic Client Registration** — there is no console to visit and no
    client to create by hand. This tool registers one.
  * **No client secret.** `token_endpoint_auth_methods_supported: ["none"]`,
    i.e. a public client. The refresh token is the entire credential, which is
    why it is treated as the whole secret.
  * **PKCE (S256) is the protection instead**, so the verifier from step 1 has
    to be carried into step 2. That is what --verifier is for.

The granted scope is `mcp:read`, which the server defines as read-only. This
platform could not write to the account through it even if it tried.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import urllib.parse
import urllib.request

from asx.ingest.investorpa import (
    AUTHORIZE_URL,
    MCP_URL,
    READ_SCOPE,
    REGISTER_URL,
    TOKEN_URL,
)

# The server has no out-of-band mode; a loopback redirect is the standard
# installed-app equivalent. The browser will fail to load it, which is fine:
# the authorization code is in the address bar by then.
REDIRECT = "http://localhost:8765/"
CLIENT_NAME = "asx-structural-alpha"


def _post_json(url: str, payload: dict, opener=None) -> dict:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with (opener or urllib.request).urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def _post_form(url: str, fields: dict, opener=None) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with (opener or urllib.request).urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def new_verifier() -> str:
    """A PKCE code verifier: 43-128 chars of unreserved characters (RFC 7636)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def register_client(opener=None) -> dict:
    """Dynamic Client Registration (RFC 7591)."""
    return _post_json(REGISTER_URL, {
        "client_name": CLIENT_NAME,
        "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": READ_SCOPE,
    }, opener=opener)


def new_state() -> str:
    """Opaque CSRF value echoed back on the redirect. Not optional: this
    server rejects an authorization request without it, and its 400 page
    names no reason, so the omission costs an hour rather than a retry."""
    return base64.urlsafe_b64encode(secrets.token_bytes(24)).decode().rstrip("=")


def consent_url(client_id: str, verifier: str, state: str | None = None) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "scope": READ_SCOPE,
        "state": state or new_state(),
        "code_challenge": challenge_for(verifier),
        "code_challenge_method": "S256",
        # RFC 8707 resource indicator. The MCP authorization profile requires
        # it: it names WHICH protected resource the token is for, so a token
        # minted for this endpoint cannot be replayed against another. The
        # value is the `resource` field of the endpoint's own
        # oauth-protected-resource metadata, not a guess.
        "resource": MCP_URL,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange(client_id: str, code: str, verifier: str, opener=None) -> dict:
    return _post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": verifier,
        # Carried into the exchange as well as the authorization request;
        # RFC 8707 §2.2 requires the two to agree.
        "resource": MCP_URL,
    }, opener=opener)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--client-id", help="from step 1")
    ap.add_argument("--verifier", help="from step 1")
    ap.add_argument("--code", help="the code from the redirect URL")
    args = ap.parse_args(argv)

    if not args.code:
        registration = register_client()
        client_id = registration.get("client_id")
        if not client_id:
            raise SystemExit(
                f"registration returned no client_id: {registration!r}")
        verifier = new_verifier()
        print("1. Open this URL, sign in to InvestorPA, and approve:\n")
        print("   " + consent_url(client_id, verifier) + "\n")
        print("2. The browser will fail to load localhost:8765 — that is")
        print("   expected. Copy the `code=` value out of the address bar.\n")
        print("3. Re-run with all three values (the verifier is single-use):\n")
        print(f"   python -m asx.ingest.investorpa_consent \\")
        print(f"       --client-id {client_id} \\")
        print(f"       --verifier {verifier} \\")
        print(f"       --code <that value>")
        return

    if not (args.client_id and args.verifier):
        raise SystemExit("--code needs --client-id and --verifier from step 1")

    payload = exchange(args.client_id, args.code, args.verifier)
    token = payload.get("refresh_token")
    if not token:
        raise SystemExit(
            f"no refresh_token in the response: {payload}. The code and the "
            f"verifier are both single-use and short-lived — re-run step 1."
        )
    print("\nSet these on the environment where `asx detect` runs.")
    print("Do not commit them and do not paste them into a chat.\n")
    print(f"  ASX_INVESTORPA_CLIENT_ID={args.client_id}")
    print(f"  ASX_INVESTORPA_REFRESH_TOKEN={token}")


if __name__ == "__main__":
    main()
