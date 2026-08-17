-- Tier 0 access decision (docs/ACCESS_DECISION.md, SPEC §5.1 as amended).
--
-- The central structural change: DETECTION and POSSESSION are separate facts.
-- An alert email tells us an announcement EXISTS (detection); the document
-- bytes arrive later via a company IR site or human-triggered capture, or
-- never. A documents row therefore begins life in 'detected' with no bytes.
--
-- This makes capture completeness measurable, which is now the platform's
-- most important operational metric: detected-but-never-possessed is a
-- visible, alarmable gap rather than a silent absence (Invariant 7).

-- Bytes are optional in 'detected'; mandatory in every later state.
ALTER TABLE documents ALTER COLUMN sha256 DROP NOT NULL;
ALTER TABLE documents ALTER COLUMN storage_path DROP NOT NULL;
ALTER TABLE documents ALTER COLUMN fetched_at DROP NOT NULL;

ALTER TABLE documents
  -- How we learned this announcement exists.
  ADD COLUMN detection_source TEXT
    CHECK (detection_source IN ('market_index_alert', 'listcorp_alert',
                                'ir_email', 'manual', 'other')),
  ADD COLUMN detected_at TIMESTAMPTZ,
  -- Idempotency key for re-reading the mailbox: stable per detected
  -- announcement (typically email Message-ID + ticker).
  ADD COLUMN detection_key TEXT,
  -- How we came to hold the bytes. NULL while still merely detected.
  ADD COLUMN possession_source TEXT
    CHECK (possession_source IN ('ir_website', 'email_attachment',
                                 'manual_capture', 'filedrop'));

CREATE UNIQUE INDEX documents_detection_key_uq ON documents (detection_key)
  WHERE detection_key IS NOT NULL;
CREATE INDEX documents_detected_idx ON documents (detected_at)
  WHERE parse_status = 'detected';

-- 'detected' precedes 'unparsed': we know of the document but hold no bytes.
ALTER TABLE documents DROP CONSTRAINT documents_parse_status_check;
ALTER TABLE documents ADD CONSTRAINT documents_parse_status_check CHECK (
  parse_status IN ('detected', 'unparsed', 'parsed', 'validated',
                   'review', 'rejected', 'not_applicable')
);

-- Bytes and their provenance are mandatory for every state that implies we
-- hold (or held) the document. Two states may legitimately be byte-free:
--   'detected'       — known to exist, not yet captured;
--   'not_applicable' — will never be parsed, so was never worth capturing
--                      (a duplicate detection, or a class no parser handles).
-- Nothing can be parsed from an announcement we do not hold.
ALTER TABLE documents ADD CONSTRAINT documents_possession_check CHECK (
  parse_status IN ('detected', 'not_applicable')
  OR (sha256 IS NOT NULL AND storage_path IS NOT NULL
      AND fetched_at IS NOT NULL AND possession_source IS NOT NULL)
);

-- Index membership, replacing the market-cap ceiling on screens while no
-- price vendor is subscribed (ACCESS_DECISION §3). Sourced from ETF issuers'
-- published daily holdings files, refreshed weekly.
--
-- Invariant 1 still applies: holdings files list TICKERS, so membership is
-- resolved to entity_id through the effective-dated listings table on load.
-- A ticker that cannot be resolved is recorded unresolved and alarms, rather
-- than being joined on the code.
CREATE TABLE index_membership (
  index_code    TEXT NOT NULL,           -- e.g. 'ASX300_PROXY_VAS'
  entity_id     BIGINT REFERENCES entities,
  ticker_as_published TEXT NOT NULL,     -- verbatim, for audit only
  as_of         DATE NOT NULL,
  knowable_at   TIMESTAMPTZ NOT NULL,    -- issuer's file publication date
  source_url    TEXT NOT NULL,
  source_note   TEXT,
  PRIMARY KEY (index_code, ticker_as_published, as_of)
);
CREATE INDEX index_membership_entity_idx ON index_membership (entity_id, as_of);

-- Manually recorded shares-on-issue, replacing the price vendor's figure in
-- the Phase 0 reconciliation (ACCESS_DECISION §3, ACCEPTANCE 0.7 as amended).
-- Hand-read from the ASX website by the owner; recorded_by and source_note
-- carry the provenance a vendor file would otherwise supply (Invariant 12).
CREATE TABLE manual_share_counts (
  entry_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id    BIGINT NOT NULL REFERENCES entities,
  class_code   TEXT   NOT NULL,
  as_at        DATE   NOT NULL,
  qty          NUMERIC NOT NULL CHECK (qty >= 0),
  recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  recorded_by  TEXT   NOT NULL DEFAULT 'owner',
  source_note  TEXT   NOT NULL,          -- where on the ASX site it was read
  UNIQUE (entity_id, class_code, as_at)
);

-- Every signal row states its coverage (Invariant 9's spirit: a screen output
-- without its coverage caveats is prohibited). Under Tier 0 the size ceiling
-- is an index-membership proxy, not a market-cap figure, and rows say so.
ALTER TABLE signal_cluster_buys
  ADD COLUMN coverage_flags TEXT[] NOT NULL DEFAULT '{}';

-- Which clock each feed's freshness/volume is measured on. Detections have
-- no fetched_at (no bytes yet), so they are measured on detected_at.
ALTER TABLE feed_slos
  ADD COLUMN time_column TEXT NOT NULL DEFAULT 'fetched_at'
    CHECK (time_column IN ('fetched_at', 'detected_at', 'lodged_at'));

-- Capture-completeness SLO: detections that never became documents are the
-- Tier 0 failure mode, so they get their own baseline (see monitor checks).
INSERT INTO feed_slos (feed_name, doc_class, max_staleness_hours,
                       min_docs_per_window, window_days, time_column, note)
VALUES ('detections_all', NULL, 72, 20, 5, 'detected_at',
        'Alert-email detections. Zero detections means the mailbox feed or the '
        'alert subscriptions have broken, not that the market is quiet.');
