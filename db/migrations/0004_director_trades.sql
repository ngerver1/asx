-- Phase 1: director transactions from Appendix 3Y / 3Z (SPEC §7).
--
-- Invariant 2: event_date is the date of the change; knowable_at is the ASX
-- lodgement timestamp. LR 3.19B allows up to five business days between them
-- (verify current rule text at go-live; SPEC §7). All signals join on
-- knowable_at.
-- Invariant 8: classification includes 'unknown' and nothing ambiguous is
-- coerced into a substantive category.

CREATE TABLE director_trades (
  trade_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id       BIGINT NOT NULL REFERENCES entities,
  person_name_raw TEXT   NOT NULL,
  person_id       BIGINT REFERENCES persons,       -- soft link; manual merge only
  doc_id          BIGINT NOT NULL REFERENCES documents,
  supersedes_doc  BIGINT REFERENCES documents,     -- amended notices; latest wins
  event_date      DATE   NOT NULL,
  knowable_at     TIMESTAMPTZ NOT NULL,
  interest_nature TEXT   CHECK (interest_nature IN ('direct','indirect','unknown')),
  indirect_detail TEXT,                            -- trust/super fund/spouse text, verbatim
  security_class  TEXT   NOT NULL,
  qty_acquired    NUMERIC,
  qty_disposed    NUMERIC,
  consideration_text TEXT,                         -- verbatim (Invariant 9: as-reported preserved)
  consideration_aud  NUMERIC,
  price_per_unit  NUMERIC,                         -- derived only where safely computable
  held_before     NUMERIC,
  held_after      NUMERIC,
  classification  TEXT NOT NULL CHECK (classification IN
    ('onmkt_buy_cash','onmkt_sell','exercise','placement_participation','spp_participation',
     'drp','rights_participation','vesting_incentive','offmkt_transfer','margin_or_forced',
     'buyback_into','other','unknown')),
  confidence      NUMERIC NOT NULL,
  review_status   TEXT NOT NULL DEFAULT 'auto'
                  CHECK (review_status IN ('auto','review','human_accepted','human_corrected')),
  superseded     BOOLEAN NOT NULL DEFAULT false    -- true once a replacement notice lands
);
CREATE INDEX director_trades_entity_idx ON director_trades (entity_id, knowable_at);
CREATE INDEX director_trades_class_idx ON director_trades (classification) WHERE NOT superseded;
CREATE INDEX director_trades_doc_idx ON director_trades (doc_id);
