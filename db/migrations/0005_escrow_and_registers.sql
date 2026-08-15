-- Phase 2 & 3 canonical schemas, created now so Phase 0/1 code can reference
-- them and reconciliations can land as soon as parsers exist (SPEC §8, §9).

CREATE TABLE escrow_parcels (
  parcel_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id      BIGINT NOT NULL REFERENCES entities,
  class_code     TEXT,                    -- restricted class where applicable
  escrow_kind    TEXT NOT NULL CHECK (escrow_kind IN ('asx_mandatory','voluntary')),
  holder_category TEXT CHECK (holder_category IN
    ('promoter','seed_capitalist','vendor','related_party','professional','employee','other','unknown')),
  holder_name_raw TEXT,
  holder_entity_id BIGINT REFERENCES entities,
  qty            NUMERIC NOT NULL,
  escrow_start   DATE,
  release_date   DATE,                    -- null for condition-based voluntary escrow
  release_condition TEXT,                 -- verbatim for milestone-based releases
  source_doc_id  BIGINT NOT NULL REFERENCES documents,
  knowable_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX escrow_parcels_entity_idx ON escrow_parcels (entity_id, release_date);

-- Top 20 register snapshots from annual reports (SPEC §9.1).
-- Invariant 2's canonical example: as_at_date (event_date) typically precedes
-- knowable_at (report release) by two to three months. Custodian nominees are
-- flagged, never pierced.
CREATE TABLE holder_snapshots (
  snapshot_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id      BIGINT NOT NULL REFERENCES entities,
  as_at_date     DATE   NOT NULL,          -- event_date
  knowable_at    TIMESTAMPTZ NOT NULL,     -- report release
  rank           SMALLINT NOT NULL,
  holder_name_raw TEXT  NOT NULL,
  holder_entity_id BIGINT REFERENCES entities,   -- resolved where non-custodian
  is_custodian   BOOLEAN NOT NULL,
  units          NUMERIC NOT NULL,
  pct_stated     NUMERIC,                  -- as printed (Invariant 9)
  source_doc_id  BIGINT NOT NULL REFERENCES documents,
  source_pages   INT[]
);
CREATE INDEX holder_snapshots_entity_idx ON holder_snapshots (entity_id, as_at_date);

-- Substantial holder notices (Forms 603/604/605, Corporations Act s671B;
-- verify rule text at parser implementation). Feed for strategic-holdings
-- estimates in float_series.
CREATE TABLE substantial_holdings (
  holding_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id      BIGINT NOT NULL REFERENCES entities,     -- the listed company
  holder_name_raw TEXT  NOT NULL,
  holder_entity_id BIGINT REFERENCES entities,
  form           TEXT   NOT NULL CHECK (form IN ('603','604','605')),
  event_date     DATE   NOT NULL,          -- date of change per the notice
  knowable_at    TIMESTAMPTZ NOT NULL,     -- lodgement
  votes          NUMERIC,
  voting_pct     NUMERIC,                  -- as printed
  source_doc_id  BIGINT NOT NULL REFERENCES documents
);
CREATE INDEX substantial_holdings_entity_idx ON substantial_holdings (entity_id, knowable_at);
