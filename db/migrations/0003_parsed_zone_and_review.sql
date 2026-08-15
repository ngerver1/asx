-- Parsed zone (versioned extraction outputs), resolver alias store, review
-- queue, and the persons soft-identity table (SPEC §3, §5.2, §6, §7).

-- Parsed zone: append-only, versioned by parser version. Reprocessing writes a
-- new (doc_id, parser_name, parser_version) row rather than overwriting
-- (Invariant 3). Re-running the same parser version on the same document is an
-- idempotent no-op enforced by the unique key.
CREATE TABLE parsed_records (
  parsed_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  doc_id         BIGINT NOT NULL REFERENCES documents,
  parser_name    TEXT   NOT NULL,
  parser_version INT    NOT NULL,
  model_id       TEXT,                  -- LLM model used, null for pure-rules parsers
  prompt_hash    CHAR(64),              -- sha256 of the prompt template
  payload        JSONB  NOT NULL,       -- extraction output, schema per parser
  confidence     NUMERIC,
  validation     JSONB,                 -- validator findings (errors/warnings)
  passes_agree   BOOLEAN,               -- dual-pass field-level agreement (SPEC §6)
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (doc_id, parser_name, parser_version)
);

-- Resolver alias store: every non-exact resolution is recorded with its method
-- and confidence so the same string never needs re-adjudication (SPEC §5.2).
CREATE TABLE entity_aliases (
  alias_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  alias_norm    TEXT   NOT NULL,
  entity_id     BIGINT NOT NULL REFERENCES entities,
  method        TEXT   NOT NULL CHECK (method IN ('exact','manual','fuzzy','llm','review')),
  confidence    NUMERIC NOT NULL,
  evidence      TEXT,                  -- e.g. fuzzy score, LLM rationale
  source_doc_id BIGINT REFERENCES documents,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (alias_norm, entity_id)
);
CREATE INDEX entity_aliases_norm_idx ON entity_aliases (alias_norm);

-- Review queue (SPEC §6): one table, resolutions write back through the same
-- validation gate as automated extraction.
CREATE TABLE review_items (
  item_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind          TEXT   NOT NULL,       -- e.g. 'extraction', 'resolution', 'reconciliation', 'classification'
  doc_id        BIGINT REFERENCES documents,
  payload       JSONB,
  reason        TEXT   NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at   TIMESTAMPTZ,
  resolution    TEXT,                  -- 'accepted','corrected','rejected'
  resolver_note TEXT
);
CREATE INDEX review_items_open_idx ON review_items (created_at) WHERE resolved_at IS NULL;

-- Persons: soft identity table keyed on normalised name, manual merge only.
-- Names collide and no DOB is available — never auto-merge (SPEC §7).
CREATE TABLE persons (
  person_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name_norm    TEXT NOT NULL,
  display_name TEXT NOT NULL,
  merged_into  BIGINT REFERENCES persons,   -- manual merges only; null = live record
  note         TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX persons_name_norm_idx ON persons (name_norm);
