#!/usr/bin/env bash
# Start a throwaway Postgres for the test suite.
#
# Without a database, 44 of the 260 tests SKIP — and a skipped test looks
# exactly like a passing one in the summary line. Every CI failure this
# project has had was a test that skipped locally and ran on the runner:
# first a missing decryption backend, then a parser schema change that only
# the database tests exercised. Both times the local suite said green.
#
#   ./tools/dev-postgres.sh start && make test-all
#
# The cluster lives under /var/lib/postgresql/asxdata and holds nothing worth
# keeping; 'reset' drops and recreates it.
set -euo pipefail

PGBIN=/usr/lib/postgresql/16/bin
PGDATA=${PGDATA:-/var/lib/postgresql/asxdata}
PORT=${PGPORT:-5432}

as_postgres() { su postgres -c "PATH=$PGBIN:\$PATH $1"; }

case "${1:-start}" in
  start)
    if pg_isready -p "$PORT" >/dev/null 2>&1; then
      echo "postgres already accepting connections on :$PORT"
    else
      mkdir -p "$PGDATA" /var/run/postgresql
      chown postgres "$PGDATA" /var/run/postgresql
      [ -s "$PGDATA/PG_VERSION" ] || \
        as_postgres "initdb -D $PGDATA -U asx --auth=trust" >/dev/null
      as_postgres "pg_ctl -D $PGDATA -o '-p $PORT' -l $PGDATA/log start" >/dev/null
      until pg_isready -p "$PORT" >/dev/null 2>&1; do sleep 1; done
    fi
    as_postgres "createdb -p $PORT -U asx asx" 2>/dev/null || true
    echo "DATABASE_URL=postgresql://asx@localhost:$PORT/asx"
    ;;
  stop)   as_postgres "pg_ctl -D $PGDATA stop" ;;
  reset)  as_postgres "pg_ctl -D $PGDATA stop" >/dev/null 2>&1 || true
          rm -rf "$PGDATA"; exec "$0" start ;;
  *)      echo "usage: $0 {start|stop|reset}" >&2; exit 2 ;;
esac
