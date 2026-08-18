-- Corrections after verifying the loaders against the real publisher files
-- (ASIC company register 202608, ASX listed companies 18/08/2026).
-- CLAUDE.md requires verifying field semantics against the primary source at
-- implementation time; doing so found two wrong assumptions and two columns
-- worth capturing.

-- 1. The ASIC column "Current Name Start Date" does NOT date the name on its
--    own row. On a FORMER-name row it carries the date the company's CURRENT
--    name began — i.e. the date that former name ceased. Renamed so the code
--    cannot keep reading it as "this name started here".
ALTER TABLE asic_registry RENAME COLUMN name_start_date TO current_name_start_date;
COMMENT ON COLUMN asic_registry.current_name_start_date IS
  'Date the company''s CURRENT name began, as published on former-name rows. '
  'Not the start date of the name on this row.';

-- 2. Deregistration date is published and is the cleanest signal that a
--    company has ceased to exist.
ALTER TABLE asic_registry ADD COLUMN deregistration_date DATE;

COMMENT ON COLUMN asic_registry.is_current_name IS
  'True only where the publisher set the indicator to Y. A BLANK indicator '
  'means a former name (~40% of rows), not a current one.';

-- 3. The ASX listed-companies file carries a listing date and a market cap
--    per company. Both are dated snapshots of free published data, stored
--    with full provenance. Market cap is genuinely absent for some companies
--    (published as '--'), which is recorded as NULL, never as zero.
--
--    Note for the owner: ACCESS_DECISION §3 replaced market-cap ceilings with
--    an index-membership proxy on the basis that no price data was available.
--    This file supplies a current market cap for free, so a live forward
--    screen COULD use a real ceiling. It remains a point-in-time snapshot with
--    no history, so it cannot support backtests, and the cluster-buy signal is
--    left on the index proxy until the owner decides otherwise.
CREATE TABLE listing_snapshots (
  entity_id      BIGINT NOT NULL REFERENCES entities,
  as_at          DATE   NOT NULL,
  ticker         TEXT   NOT NULL,
  market_cap_aud NUMERIC,              -- NULL where the publisher printed '--'
  sector         TEXT,
  listing_date   DATE,
  source_load_id BIGINT NOT NULL REFERENCES reference_loads,
  PRIMARY KEY (entity_id, as_at)
);
CREATE INDEX listing_snapshots_as_at_idx ON listing_snapshots (as_at);
