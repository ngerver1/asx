-- Multi-part reference extracts: record EVERY part in the raw zone.
--
-- The ASIC company register is published as 14 numbered files. The loader
-- treated them as one logical load (correct — a company's name records
-- straddle part boundaries) but stored only the first part's bytes in the raw
-- zone. That breaks the prime directive: derived data must be regenerable
-- from raw, and 13/14 of the input was not kept.
--
-- reference_loads.doc_id stays as the FIRST part, so existing foreign keys and
-- the (source, doc_id) idempotency key are unchanged. This table records the
-- full part list, which is what makes the load reproducible and what lets a
-- refresh notice that part 7 changed even though part 1 did not.
CREATE TABLE reference_load_parts (
  load_id  BIGINT NOT NULL REFERENCES reference_loads ON DELETE CASCADE,
  part_no  INT    NOT NULL,          -- 1-based, in the order supplied
  doc_id   BIGINT NOT NULL REFERENCES documents,
  filename TEXT   NOT NULL,          -- publisher's filename, for human triage
  PRIMARY KEY (load_id, part_no),
  UNIQUE (load_id, doc_id)
);
CREATE INDEX reference_load_parts_doc_idx ON reference_load_parts (doc_id);

COMMENT ON TABLE reference_load_parts IS
  'One row per file in a reference load. Single-file loads get exactly one '
  'row. A load is only "already applied" when the stored part set matches the '
  'files being offered — an unchanged part 1 is not evidence of an unchanged '
  'extract.';
