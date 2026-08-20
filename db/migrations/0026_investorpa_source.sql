-- investorpa.com as a detection and possession source.
--
-- Assessed and declined for automated use on 20 Aug 2026 (docs/SOURCE_INVESTORPA.md)
-- because its terms could not be read from this network. Both halves of that
-- have since changed: the host is reachable, and investorpa.com/features/
-- publishes a Remote MCP Server as a product feature, naming Claude Code as a
-- supported client. That is the recorded basis; see fetch_guard.DECLARED_SOURCES.
--
-- Three CHECK constraints have to widen together, because a source that can be
-- detected but not possessed, or possessed but not dated, is a source that
-- fails halfway through its first row.

-- 1. detection_source. This one is a live bug, not a new feature: mailbox.py
--    has emitted 'investorpa_alert' since the sender rule was added, against a
--    constraint from 0008 that never listed it. The first real alert would
--    raise CheckViolation, be swallowed by cmd_detect's broad except, and be
--    counted as a generic failure -- so docs/SOURCE_INVESTORPA.md's claim that
--    such emails "are ingested the moment any arrive" has never been true.
--
--    The value is 'investorpa', not 'investorpa_alert': the API and the alert
--    email are the same source observed two ways, and splitting them would
--    make "did InvestorPA tell us about this announcement" two questions.
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_detection_source_check;
ALTER TABLE documents ADD CONSTRAINT documents_detection_source_check
  CHECK (detection_source = ANY (ARRAY[
    'market_index_alert',
    'listcorp_alert',
    'investorpa',
    'ir_email',
    'manual',
    'other'
  ]));

-- 2. lodged_at_source. InvestorPA states a release timestamp to the second
--    (time_release), which is the exchange's publication time as a third party
--    observed it -- the same category of fact as a Market Index alert, and
--    strictly better than pdf_creation, which runs about six minutes early
--    (0019). It is NOT 'asx': that value stays reserved for the exchange's own
--    feed, and a re-host is not the exchange.
--
--    listcorp_alert and ir_email are added at the same time, and not because
--    anything new needs them: detection.py has been stamping EVERY sender's
--    parsed timestamp as 'market_index_alert' since 0019 shipped, because
--    those were the only alert values the constraint allowed. That made the
--    column state a falsehood on the platform's load-bearing provenance
--    field. A timestamp read from a Listcorp or IR email now says so.
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_lodged_at_source_check;
ALTER TABLE documents ADD CONSTRAINT documents_lodged_at_source_check
  CHECK (lodged_at_source = ANY (ARRAY[
    'market_index_alert',
    'listcorp_alert',
    'investorpa',
    'ir_email',
    'pdf_creation',
    'asx',
    'manual'
  ]));

-- 3. possession_source. Kept distinct from 'ir_website' for the same reason
--    0017 kept 'asx_targeted' distinct from 'manual_capture': "which documents
--    did the platform fetch, and from whom" must be answerable from the data
--    rather than from memory.
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_possession_source_check;
ALTER TABLE documents ADD CONSTRAINT documents_possession_source_check
  CHECK (possession_source = ANY (ARRAY[
    'ir_website',
    'asx_targeted',
    'investorpa',
    'email_attachment',
    'manual_capture',
    'filedrop',
    'reference_download'
  ]));

COMMENT ON COLUMN documents.detection_source IS
  'How we learned the announcement exists. Two feeds run in parallel by '
  'decision (20 Aug 2026): market_index_alert is watchlist-bounded at ~200 '
  'codes, investorpa covers the whole exchange. Keeping both is what makes '
  'detection coverage measurable rather than merely asserted.';
