-- The raw zone has to survive the container.
--
-- Documents are stored on a local filesystem (ASX_RAW_ROOT, default
-- data/raw), which in a cloud container is wiped between sessions. Every
-- read_document call would therefore fail after a restart, and the only
-- durable copies of anything were the fixtures that happened to be committed
-- to git. A raw zone that does not survive is not a raw zone.
--
-- Postgres is the only durable store this platform has, and PDFs do not fit
-- in it: 320 KB each, ~1.6 GB across a 24-month backfill. Their extracted
-- text does — 4.7 KB each, 1.4 KB compressed, about 7 MB for the same
-- backfill. 225x smaller, and sufficient: the rules parser reads text and
-- never touches layout.
--
-- So the text is the durable artifact and the file is a cache. read_document
-- prefers the file when the disk still has it and falls back to the text when
-- it does not, which means a restart costs nothing.
--
-- What is deliberately NOT changed: sha256 stays the hash of the ORIGINAL
-- document. It is the identity of the source artifact and what dedupe keys
-- on. Hashing the text instead would collide two different lodgements that
-- extract to the same characters — amended notices routinely do — and would
-- change every doc_id whenever the extraction library was upgraded.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS document_text TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS text_sha256 CHAR(64);

-- Which extractor produced it. A text layer is a reading of the document, not
-- the document (Invariant 6), so a later library that reads a page better
-- must be able to find what the old one produced and supersede it.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS text_extractor TEXT;

-- A document held only as text has no file to point at.
ALTER TABLE documents ALTER COLUMN storage_path DROP NOT NULL;

-- Text and its checksum travel together or not at all: an unverifiable text
-- layer is indistinguishable from a corrupted one.
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_text_checksum;
ALTER TABLE documents ADD CONSTRAINT documents_text_checksum CHECK (
  (document_text IS NULL) = (text_sha256 IS NULL)
);

-- Possession still means holding SOMETHING. The Tier 0 check demanded a
-- storage_path; a document held as text satisfies it just as well, and a
-- document held as neither does not.
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_possession_check;
ALTER TABLE documents ADD CONSTRAINT documents_possession_check CHECK (
  parse_status IN ('detected', 'not_applicable')
  OR (sha256 IS NOT NULL AND fetched_at IS NOT NULL
      AND possession_source IS NOT NULL
      AND (storage_path IS NOT NULL OR document_text IS NOT NULL))
);
