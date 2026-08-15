-- Phase 0: entity master and document index (SPEC §5.2, §5.3).
--
-- Invariant 1: the canonical key everywhere is entity_id, anchored to ACN where
-- one exists. Tickers live in the effective-dated `listings` table and are never
-- primary or join keys.
-- Invariant 3: `documents` indexes the append-only raw zone; rows are inserted
-- once per unique sha256 and never deleted.

CREATE TABLE entities (
  entity_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  acn            CHAR(9) UNIQUE,          -- null only for foreign-incorporated listcos and persons
  abn            CHAR(11),
  entity_kind    TEXT NOT NULL CHECK (entity_kind IN ('company','trust','stapled','foreign','person','other')),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE documents (
  doc_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source           TEXT NOT NULL,             -- provider identifier
  source_ref       TEXT,                      -- provider's id/url
  entity_id        BIGINT REFERENCES entities,-- null until resolved
  ticker_as_lodged TEXT,                      -- verbatim, for audit only; never a join key
  title            TEXT,
  asx_doc_types    TEXT[],                    -- ASX-assigned type codes if provided
  doc_class        TEXT,                      -- our classifier output (SPEC §5.3 taxonomy)
  price_sensitive  BOOLEAN,
  lodged_at        TIMESTAMPTZ,               -- knowable_at for its contents
  fetched_at       TIMESTAMPTZ NOT NULL,
  sha256           CHAR(64) NOT NULL,
  storage_path     TEXT NOT NULL,
  parse_status     TEXT NOT NULL DEFAULT 'unparsed'
                   CHECK (parse_status IN ('unparsed','parsed','validated','review','rejected','not_applicable'))
);
CREATE UNIQUE INDEX documents_sha256_idx ON documents (sha256);
CREATE INDEX documents_lodged_at_idx ON documents (lodged_at);
CREATE INDEX documents_doc_class_idx ON documents (doc_class);
CREATE INDEX documents_parse_status_idx ON documents (parse_status);

CREATE TABLE entity_names (
  entity_id      BIGINT NOT NULL REFERENCES entities,
  name           TEXT   NOT NULL,
  name_norm      TEXT   NOT NULL,         -- asx.ids.normalize.name_norm(), the one normaliser
  name_kind      TEXT   NOT NULL CHECK (name_kind IN ('legal','former','trading','alias')),
  valid_from     DATE   NOT NULL,
  valid_to       DATE,                    -- null = current
  source_doc_id  BIGINT REFERENCES documents
);
CREATE INDEX entity_names_norm_idx ON entity_names (name_norm);
CREATE INDEX entity_names_entity_idx ON entity_names (entity_id);

CREATE TABLE listings (
  listing_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id      BIGINT NOT NULL REFERENCES entities,
  exchange       TEXT   NOT NULL DEFAULT 'ASX',
  ticker         TEXT   NOT NULL,
  security_class TEXT   NOT NULL DEFAULT 'ORD',   -- ORD, restricted classes, notes, etc.
  valid_from     DATE   NOT NULL,
  valid_to       DATE,
  source         TEXT   NOT NULL DEFAULT 'announcement'
                 CHECK (source IN ('announcement','asx_file','vendor','manual')),
  source_doc_id  BIGINT REFERENCES documents
);
CREATE INDEX listings_ticker_idx ON listings (exchange, ticker, valid_from);
CREATE INDEX listings_entity_idx ON listings (entity_id);

-- Invariant 4: universe membership is effective-dated; delisted entities are
-- permanent citizens. Sub-universes are dated queries over this table, never
-- static lists.
CREATE TABLE universe_membership (
  entity_id     BIGINT NOT NULL REFERENCES entities,
  listed_from   DATE   NOT NULL,
  listed_to     DATE,                     -- null = currently listed
  delist_reason TEXT,                     -- acquired / failed / voluntary / unknown
  source_doc_id BIGINT REFERENCES documents,
  PRIMARY KEY (entity_id, listed_from)
);
