-- Conviction sizing: a director backing themselves, relative to what they
-- already held (SPEC §7, second of the three named derived signals).
--
-- Cluster buying asks how MANY directors bought. It cannot see one director
-- buying heavily, and on the current corpus that is what it misses: a
-- $896,000 on-market purchase that more than doubled the buyer's own stake
-- scored nothing, while a two-director cluster totalling $2,402 sat at the
-- top of the screen. Both are real signals; they are not the same signal, and
-- loosening the cluster rule to one director would have destroyed what
-- "cluster" means rather than adding this one.
--
-- The measure is qty_acquired / held_before — the proportional increase in
-- the director's own position. Deliberately NOT the dollar amount: a $500,000
-- purchase against an 83-million-share holding moves that person's exposure
-- by 0.1% and tells you almost nothing, while $27,000 that doubles a small
-- holding is someone changing their mind. Dollars are recorded so a reader
-- can rank on them, but they do not decide who appears.
--
-- No price data is needed for this, which is why it is buildable now while
-- the market-cap ceiling is not (ACCESS_DECISION §3).
CREATE TABLE signal_conviction_buys (
  signal_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  signal_version  INT    NOT NULL,
  entity_id       BIGINT NOT NULL REFERENCES entities,
  -- Provenance back to the exact lodgement this came from (Invariant 12).
  trade_id        BIGINT NOT NULL REFERENCES director_trades,
  person_name_raw TEXT   NOT NULL,
  event_date      DATE   NOT NULL,
  -- The trade's own lodgement: a single-notice signal is actionable as soon
  -- as that notice is public, unlike a cluster which waits for its last
  -- member (Invariant 2).
  knowable_at     TIMESTAMPTZ NOT NULL,
  consideration_aud NUMERIC,
  qty_acquired    NUMERIC NOT NULL,
  held_before     NUMERIC NOT NULL CHECK (held_before > 0),
  -- qty_acquired / held_before. 1.0 means the director doubled their holding.
  stake_increase  NUMERIC NOT NULL,
  coverage_flags  TEXT[] NOT NULL,
  built_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX signal_conviction_buys_entity_idx
  ON signal_conviction_buys (entity_id, knowable_at);
CREATE UNIQUE INDEX signal_conviction_buys_trade_idx
  ON signal_conviction_buys (signal_version, trade_id);
