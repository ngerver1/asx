-- Where a document's lodgement timestamp came from.
--
-- knowable_at is the load-bearing column of this whole platform (Invariant 2):
-- every analytic joins on it, and a signal computed against a timestamp we
-- guessed is a backtest of a fact nobody could have known. So the timestamp
-- has to say where it came from.
--
--   market_index_alert  the alert email's published time for this
--                       announcement. Observed by a third party, arriving
--                       within minutes of the document being generated.
--   pdf_creation        the timestamp inside the PDF. This is when the file
--                       was produced, which is NOT by definition when the
--                       announcement was released -- measured against alerts
--                       on documents where both exist, it runs about six
--                       minutes early and never late. Good enough for a
--                       daily-resolution signal, and wrong to record as if
--                       it were the exchange's own stamp.
--   asx                 reserved: the exchange's published release time,
--                       should a licensed feed ever supply it.
--   manual              a human typed it in, after looking.
--
-- Nothing may be inferred into this column. A document with no timestamp
-- keeps lodged_at NULL and produces no canonical rows, because a fact with no
-- knowable_at is not a fact this platform can carry.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS lodged_at_source TEXT
  CHECK (lodged_at_source IN ('market_index_alert', 'pdf_creation', 'asx', 'manual'));

-- A timestamp must always say where it came from, and a source must always
-- have a timestamp to describe.
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_lodged_at_provenance;
ALTER TABLE documents ADD CONSTRAINT documents_lodged_at_provenance CHECK (
  (lodged_at IS NULL) = (lodged_at_source IS NULL)
);
