-- When each signal row first appeared, so the published screen can show what
-- is NEW rather than only what currently qualifies.
--
-- Why this is its own table rather than a column on the signal tables.
--
-- The signal tables are derived and regenerable: build-signals DELETEs every
-- row for the version and rewrites it from director_trades. A column on them
-- would be destroyed on every rebuild. Worse, they are deliberately absent
-- from the state snapshot (state/*.csv), so a fresh container rebuilds them
-- from nothing — a first_seen column there would mark the entire screen as
-- new on every restore, which is precisely the false alarm this exists to
-- prevent.
--
-- "When did we first see this" is not derived data. It is an observation
-- about our own history, it cannot be recomputed from the corpus, and losing
-- it is not recoverable by reprocessing. So it is stored as a fact, keyed on
-- something stable, and it IS snapshotted.
--
-- The natural keys:
--   cluster     entity_id:window_start  — "the cluster at this company that
--               began with this buy". window_end and the member list grow as
--               directors join, so neither can be part of the identity.
--   conviction  trade_id — already unique per signal_version on the signal
--               table itself, and a conviction row IS one trade.
--
-- A row that qualifies, drops out, and later qualifies again keeps its
-- original first_seen_at and does not re-announce itself. That is deliberate:
-- the common cause of dropping out is a review status changing, and a row
-- flickering in and out of the "new" table would train the reader to ignore
-- it.
CREATE TABLE signal_first_seen (
    signal_version integer     NOT NULL,
    kind           text        NOT NULL CHECK (kind IN ('cluster', 'conviction')),
    natural_key    text        NOT NULL,
    first_seen_at  timestamptz NOT NULL,
    -- TRUE means "already on the screen when tracking began, actual arrival
    -- date unknown". Every row that exists at migration time gets this.
    --
    -- The alternative was to backfill a plausible date from built_at, and it
    -- was tried and reverted: built_at is rewritten on EVERY rebuild, so it
    -- records the most recent build rather than the first, and backfilling
    -- from it announced all 37 existing rows as new — the exact false alarm
    -- the table exists to prevent, shipped by the table meant to prevent it.
    --
    -- We do not know when these rows arrived. Nothing recorded it. So the
    -- column says "unknown" rather than asserting a date that would be wrong
    -- (CLAUDE.md: ambiguous → 'unknown', never a substantive default), and
    -- the screen reports them as predating tracking rather than as new.
    backfilled     boolean     NOT NULL DEFAULT false,
    PRIMARY KEY (signal_version, kind, natural_key)
);

COMMENT ON TABLE signal_first_seen IS
    'When a signal row was first built. Durable and snapshotted: the signal '
    'tables themselves are regenerable and are rebuilt from empty in a fresh '
    'container, so this cannot live on them. backfilled = arrival date '
    'unknown because the row predates this table.';

INSERT INTO signal_first_seen
       (signal_version, kind, natural_key, first_seen_at, backfilled)
SELECT signal_version, 'cluster',
       entity_id || ':' || window_start, now(), true
  FROM signal_cluster_buys
 GROUP BY signal_version, entity_id, window_start
ON CONFLICT DO NOTHING;

INSERT INTO signal_first_seen
       (signal_version, kind, natural_key, first_seen_at, backfilled)
SELECT signal_version, 'conviction', trade_id::text, now(), true
  FROM signal_conviction_buys
 GROUP BY signal_version, trade_id
ON CONFLICT DO NOTHING;

-- ...except where the arrival date IS known.
--
-- A row that qualifies ONLY because of a document obtained on the day this
-- table was created did arrive that day, and we can prove which ones: remove
-- today's documents and see whether the row still stands up. That is
-- evidence, not the guess the paragraph above refuses to make, so those rows
-- get a real arrival date.
--
-- On a database that already holds a state snapshot this is a no-op: the
-- restore loads signal_first_seen from CSV and the INSERTs above add nothing.
-- It matters exactly once, in the database where tracking began.

-- Conviction: one row IS one trade, so it arrived when its document did.
UPDATE signal_first_seen fs
   SET backfilled = false
  FROM signal_conviction_buys s
  JOIN director_trades t ON t.trade_id = s.trade_id
  JOIN documents d       ON d.doc_id   = t.doc_id
 WHERE fs.kind = 'conviction'
   AND fs.signal_version = s.signal_version
   AND fs.natural_key = s.trade_id::text
   AND d.fetched_at::date = now()::date;

-- Cluster: it arrived today only if today's documents are what took it to two
-- directors. A cluster that merely GAINED a member today is not new.
UPDATE signal_first_seen fs
   SET backfilled = false
 WHERE fs.kind = 'cluster'
   AND EXISTS (
       SELECT 1
         FROM signal_cluster_buys s
         JOIN director_trades t ON t.trade_id = ANY(s.trade_ids)
         JOIN documents d       ON d.doc_id   = t.doc_id
        WHERE fs.signal_version = s.signal_version
          AND fs.natural_key = s.entity_id || ':' || s.window_start
        GROUP BY s.signal_version, s.entity_id, s.window_start
       HAVING count(DISTINCT t.person_id)
              FILTER (WHERE d.fetched_at::date < now()::date) < 2);
