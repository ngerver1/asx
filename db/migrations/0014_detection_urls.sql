-- Record both halves of an alert email's links.
--
-- The mailbox ingester was parsing URLs out of every alert and then throwing
-- them away: record_detection never persisted them, and the asx.com.au ones
-- were discarded at parse time rather than kept. Both directions were wrong.
--
--   manual_open_urls     links the owner must open PERSONALLY. Under the Tier
--                        0 access decision these are exactly the ones no
--                        automated device may fetch, which is why they are
--                        stored rather than dropped — the access decision
--                        promises they are "recorded on the detection so the
--                        owner can open them personally", and `asx worklist`
--                        now prints them. A capture route that makes the
--                        owner re-find each announcement by hand is the route
--                        that drives the capture rate below the 90% floor in
--                        ACCESS_DECISION §5.
--
--   fetch_candidate_urls an ALLOWLISTED subset that may be fetched
--                        automatically: company IR documents. Named for what
--                        it is. "Not on asx.com.au" is NOT the test — an
--                        alert body also carries the provider's own tracking,
--                        preferences and unsubscribe links, and fetching the
--                        first of those would store an HTML confirmation page
--                        as the announcement and could unsubscribe the owner
--                        from the platform's only detection source.
ALTER TABLE documents ADD COLUMN manual_open_urls     TEXT[];
ALTER TABLE documents ADD COLUMN fetch_candidate_urls TEXT[];

COMMENT ON COLUMN documents.manual_open_urls IS
  'Links for the owner to open personally. NEVER passed to any fetcher.';
COMMENT ON COLUMN documents.fetch_candidate_urls IS
  'Allowlisted company-IR document links that automated capture may fetch. '
  'An empty list is the correct and expected result for an alert-aggregator '
  'email, not a parsing failure.';
