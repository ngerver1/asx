-- The ASX's own announcement number, where a source exposes it.
--
-- Market Index alerts carry it on the end of their announcement URL
-- (.../final-directors-interest-notice-2A1690214). It is a better identity
-- than anything else available at detection time:
--
--   * The Message-ID belongs to Market Index's mail provider. It changes if
--     an alert is resent and is meaningless to any other source.
--   * The announcement number is assigned by the ASX. Two different alert
--     providers reporting the same lodgement agree on it, so it deduplicates
--     ACROSS sources — the one thing an ESP identifier can never do.
--   * A document captured days later can be tied back to the detection that
--     predicted it, which is what makes the detection/possession split
--     auditable rather than merely recorded.
ALTER TABLE documents ADD COLUMN asx_announcement_id TEXT;

CREATE UNIQUE INDEX documents_announcement_id_uq
  ON documents (asx_announcement_id)
  WHERE asx_announcement_id IS NOT NULL;

COMMENT ON COLUMN documents.asx_announcement_id IS
  'ASX announcement number as published by the source (e.g. 2A1690214). '
  'Unique where known: the same announcement must never occupy two rows, '
  'whichever alert provider reported it.';
