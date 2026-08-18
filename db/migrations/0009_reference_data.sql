-- Reference-data loading: ASIC company registry, ABN bulk extract, and the
-- ASX listed-companies file (SPEC §4 reference sources).
--
-- Design note. The ASIC dataset carries ~3M registered Australian companies;
-- the entity master must NOT contain all of them. `asic_registry` is a
-- reference/staging table used to attach an ACN to entities we actually
-- track (ASX-listed companies now, subsidiaries in Phase 3). Only companies
-- we track become `entities` rows.

-- Reference source files are stored in the append-only raw zone like any
-- other document, so every derived name and listing traces to the exact file
-- version it came from (Invariant 12). They are never parsed as disclosures.
ALTER TABLE documents DROP CONSTRAINT documents_possession_source_check;
ALTER TABLE documents ADD CONSTRAINT documents_possession_source_check CHECK (
  possession_source IN ('ir_website', 'email_attachment', 'manual_capture',
                        'filedrop', 'reference_download')
);

-- One row per reference file successfully loaded. Idempotency is on doc_id:
-- the same bytes are one document, so re-running a load is detectable.
CREATE TABLE reference_loads (
  load_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source      TEXT NOT NULL CHECK (source IN
              ('asic_companies', 'abn_bulk_extract', 'asx_listed_companies')),
  doc_id      BIGINT NOT NULL REFERENCES documents,
  -- The publisher's extract date. This is the reference-data analogue of
  -- knowable_at: nothing in a file may be treated as known before it.
  as_at       DATE NOT NULL,
  loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  row_count   INT,
  applied     BOOLEAN NOT NULL DEFAULT false,
  notes       TEXT,
  UNIQUE (source, doc_id)
);

-- ASIC company registry (reference, not the entity master).
-- The dataset carries historical names with a current-name indicator and a
-- name start date, which is what lets entity_names be effective-dated
-- honestly rather than back-dated to an assumed epoch.
CREATE TABLE asic_registry (
  acn             CHAR(9) NOT NULL,
  name            TEXT NOT NULL,
  name_norm       TEXT NOT NULL,
  is_current_name BOOLEAN NOT NULL,
  name_start_date DATE,
  status          TEXT,
  company_type    TEXT,
  company_class   TEXT,
  registration_date DATE,
  abn             CHAR(11),
  load_id         BIGINT NOT NULL REFERENCES reference_loads,
  PRIMARY KEY (acn, name_norm)
);
CREATE INDEX asic_registry_name_norm_idx ON asic_registry (name_norm);
CREATE INDEX asic_registry_current_idx ON asic_registry (name_norm)
  WHERE is_current_name;

-- Effective-dating convention, stated once for the whole platform:
--   valid_to / listed_to are INCLUSIVE (the last date the row was true);
--   NULL means still current.
-- Every lookup uses `valid_from <= d AND (valid_to IS NULL OR valid_to >= d)`,
-- so a successor row must open on the day AFTER its predecessor closes.
COMMENT ON COLUMN listings.valid_to IS
  'Inclusive: last date this listing was true. NULL = still listed.';
COMMENT ON COLUMN entity_names.valid_to IS
  'Inclusive: last date this name was true. NULL = current.';
COMMENT ON COLUMN universe_membership.listed_to IS
  'Inclusive: last date listed. NULL = still listed.';

-- Provenance for effective-dated rows built from reference files.
ALTER TABLE entity_names ADD COLUMN source_load_id BIGINT REFERENCES reference_loads;
ALTER TABLE listings     ADD COLUMN source_load_id BIGINT REFERENCES reference_loads;
ALTER TABLE universe_membership ADD COLUMN source_load_id BIGINT REFERENCES reference_loads;

-- entity_names has no natural key today; loading the same file twice must not
-- duplicate rows. One row per (entity, normalised name, start date).
CREATE UNIQUE INDEX entity_names_uq
  ON entity_names (entity_id, name_norm, valid_from);

-- One open ASX listing per (entity, ticker, class) at a time.
CREATE UNIQUE INDEX listings_open_uq
  ON listings (entity_id, exchange, ticker, security_class)
  WHERE valid_to IS NULL;

-- Ticker collisions are the Invariant 1 failure this schema exists to
-- prevent: the same code must never be open for two entities at once.
CREATE UNIQUE INDEX listings_ticker_open_uq
  ON listings (exchange, ticker, security_class)
  WHERE valid_to IS NULL;
