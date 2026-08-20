#!/bin/bash
# SessionStart hook — bring a Claude Code on the web session up to the point
# where `make test` can actually run.
#
# Three things bite a fresh container, in this order:
#   1. the package and its dev extras are not installed;
#   2. the image's Debian cryptography lacks _cffi_backend, which silently
#      takes out the pypdf AES backend — and 55 of the 60 captured ASX
#      announcement PDFs are AES-encrypted (see pyproject.toml). This has
#      cost the project a red CI run before (tools/dev-postgres.sh);
#   3. there is no database, so 44 tests skip — and a skipped test is
#      indistinguishable from a passing one (tools/dev-postgres.sh).
set -euo pipefail

# Local-only sessions have their own environment; don't touch them.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-/home/user/asx}"

LOCAL_URL="postgresql://asx@localhost:5432/asx"

echo "== asx session setup =="

# 1. Dependencies.
pip install -q -e ".[dev]"

# 2. Decryption backend. pip treats the Debian-packaged cryptography as
#    satisfying pypdf[crypto], so the extra alone does not fix this; cffi is
#    what supplies the missing _cffi_backend module.
pip install -q cffi
if python3 -c "from pypdf._crypt_providers import crypt_provider" 2>/dev/null; then
  echo "pdf decryption backend: ok"
else
  echo "WARNING: pypdf crypto backend unavailable — encrypted ASX PDFs will fail to parse."
fi

# 3. Database. Never clobber a DATABASE_URL that actually works: probe the
#    configured one first and only fall back to the local cluster if it is
#    genuinely unreachable.
probe_configured_db() {
  python3 - <<'PY'
import os, socket, sys
from urllib.parse import urlparse
url = os.environ.get("DATABASE_URL", "")
if not url:
    sys.exit(2)
u = urlparse(url)
host, port = u.hostname, u.port or 5432
if not host:
    sys.exit(2)
print(f"{host}:{port}", end="")
try:
    socket.create_connection((host, port), timeout=6).close()
except OSError:
    sys.exit(1)
PY
}

./tools/dev-postgres.sh start >/dev/null
echo "local postgres: running on :5432"

set +e
target=$(probe_configured_db)
probe_rc=$?
set -e

case "$probe_rc" in
  0) DB_URL="$DATABASE_URL"
     echo "database: using configured DATABASE_URL ($target reachable)" ;;
  2) DB_URL="$LOCAL_URL"
     echo "database: DATABASE_URL unset — using local cluster" ;;
  *) DB_URL="$LOCAL_URL"
     echo "database: configured DATABASE_URL ($target) is NOT reachable from this sandbox."
     echo "          Sandbox egress is HTTPS/443 only and allowlisted; PostgreSQL's wire"
     echo "          protocol needs raw TCP, so a managed host such as Neon cannot be"
     echo "          reached from here even after allowlisting it. Falling back to the"
     echo "          local cluster so the suite can run."
     echo "          Run migrations against the real database from a networked machine."
     # The Makefile uses 'DATABASE_URL ?=', so the unreachable value would win
     # and hang every target. Override it for this session only.
     if [ -n "${CLAUDE_ENV_FILE:-}" ] && ! grep -qs "^export DATABASE_URL=$LOCAL_URL$" "$CLAUDE_ENV_FILE"; then
       echo "export DATABASE_URL=$LOCAL_URL" >> "$CLAUDE_ENV_FILE"
     fi ;;
esac

DATABASE_URL="$DB_URL" python -m asx.cli migrate

echo "ready: DATABASE_URL=$DB_URL"
