-- Phase 0: security master and share-count history (SPEC §5.4).
--
-- Invariant 5: corporate actions are events, not overwrites. shares_outstanding
-- is a derived replay over share_events from an anchored opening balance —
-- never a stored column that pipelines overwrite.

CREATE TABLE security_classes (
  entity_id   BIGINT NOT NULL REFERENCES entities,
  class_code  TEXT   NOT NULL,        -- e.g. ORD ticker, restricted-class code, notes code
  class_kind  TEXT   NOT NULL CHECK (class_kind IN ('ordinary','restricted','option','note','pref','other')),
  description TEXT,
  valid_from  DATE NOT NULL,
  valid_to    DATE,
  source_doc_id BIGINT REFERENCES documents,
  PRIMARY KEY (entity_id, class_code, valid_from)
);

-- Anchored opening balances the replay starts from. One anchor per
-- (entity, class) is normal; a later anchor supersedes earlier events for
-- replay purposes (used when history before a certain date is unavailable).
CREATE TABLE share_anchors (
  anchor_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id     BIGINT NOT NULL REFERENCES entities,
  class_code    TEXT   NOT NULL,
  anchor_date   DATE   NOT NULL,
  qty           NUMERIC NOT NULL CHECK (qty >= 0),
  knowable_at   TIMESTAMPTZ NOT NULL,
  source_doc_id BIGINT REFERENCES documents,
  UNIQUE (entity_id, class_code, anchor_date)
);

CREATE TABLE share_events (
  event_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id    BIGINT NOT NULL REFERENCES entities,
  class_code   TEXT   NOT NULL,
  event_kind   TEXT   NOT NULL CHECK (event_kind IN
               ('quotation','issue_proposed','buyback_cancel','consolidation','split',
                'escrow_release_reclass','delist_cancel','other')),
  event_date   DATE   NOT NULL,
  knowable_at  TIMESTAMPTZ NOT NULL,
  qty_delta    NUMERIC,               -- signed share count change, null for ratio events
  ratio_num    NUMERIC,               -- e.g. consolidation 1:10 -> num=1, den=10
  ratio_den    NUMERIC,
  source_doc_id BIGINT NOT NULL REFERENCES documents,
  -- ratio events carry a ratio and no qty; delta events the reverse.
  CHECK (
    (event_kind IN ('consolidation','split') AND ratio_num IS NOT NULL AND ratio_den IS NOT NULL AND ratio_den <> 0 AND qty_delta IS NULL)
    OR
    (event_kind NOT IN ('consolidation','split') AND ratio_num IS NULL AND ratio_den IS NULL)
  )
);
CREATE INDEX share_events_entity_idx ON share_events (entity_id, class_code, event_date);
CREATE INDEX share_events_knowable_idx ON share_events (knowable_at);

-- Weekly reconciliation results: replayed count vs vendor / annual-report
-- figures (SPEC §5.4). Discrepancies beyond tolerance open review items.
CREATE TABLE share_reconciliations (
  recon_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id     BIGINT NOT NULL REFERENCES entities,
  class_code    TEXT   NOT NULL,
  as_of         DATE   NOT NULL,
  replayed_qty  NUMERIC,
  vendor_qty    NUMERIC,
  report_qty    NUMERIC,
  rel_diff      NUMERIC,              -- |replayed - vendor| / vendor
  within_tolerance BOOLEAN,
  checked_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
