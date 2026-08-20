-- Cross-feed coverage: what each detection feed saw, and what only one saw.
--
-- Two feeds run in parallel by decision (20 Aug 2026). Market Index is
-- watchlist-bounded at ~200 codes; investorpa searches the whole exchange.
-- docs/ACCEPTANCE.md records that detection coverage is currently
-- "unmeasured, not complete and not partial" — two feeds are what make it
-- measurable, and only if something actually compares them.
--
-- WHY THIS IS NOT A JOIN ON asx_announcement_id, which would be the obvious
-- key: investorpa does not expose the exchange's announcement number. It
-- exposes its own publication counter, and putting that in
-- documents.asx_announcement_id would make rows from the two feeds collide or
-- diverge at random. So the join is on the lodgement itself.
--
-- Both feeds report the same ASX release timestamp — Market Index to the
-- minute ("Published: 20/08/26, 11:59am"), investorpa to the second
-- (time_release) — so the pair (entity, minute of lodgement) identifies a
-- lodgement across them. Entity, never ticker (Invariant 1).
--
-- A row per lodgement, so the three buckets are countable:
--   both            corroborated by two independent feeds
--   investorpa_only the watchlist gap, i.e. what the old feed could not see
--   market_index_only  the interesting one. investorpa is expected to be a
--                      superset; a non-empty bucket here says it is not, and
--                      that is the whole reason for keeping both feeds rather
--                      than an assertion that one wins.
CREATE OR REPLACE VIEW detection_feed_coverage AS
WITH lodgements AS (
    SELECT
        entity_id,
        date_trunc('minute', lodged_at) AS lodged_minute,
        min(doc_class)                  AS doc_class,
        min(ticker_as_lodged)           AS ticker_as_lodged,
        count(*)                        AS document_rows,
        array_agg(DISTINCT detection_source) FILTER (
            WHERE detection_source IS NOT NULL)          AS feeds,
        array_agg(doc_id ORDER BY doc_id)                AS doc_ids,
        bool_or(parse_status NOT IN ('detected', 'not_applicable')) AS any_held
    FROM documents
    WHERE lodged_at IS NOT NULL
      AND entity_id IS NOT NULL
    GROUP BY entity_id, date_trunc('minute', lodged_at)
)
SELECT
    entity_id,
    lodged_minute,
    doc_class,
    ticker_as_lodged,
    feeds,
    doc_ids,
    document_rows,
    any_held,
    CASE
        WHEN 'market_index_alert' = ANY (feeds) AND 'investorpa' = ANY (feeds)
            THEN 'both'
        WHEN 'investorpa' = ANY (feeds)         THEN 'investorpa_only'
        WHEN 'market_index_alert' = ANY (feeds) THEN 'market_index_only'
        ELSE 'other_source'
    END AS coverage,
    -- One lodgement, more than one documents row. Expected whenever both
    -- feeds see it, because their detection keys are different by
    -- construction. Named rather than hidden: if both rows are ever parsed,
    -- the same director purchase enters director_trades twice and inflates
    -- the cluster signal, which is the failure this column exists to make
    -- visible before it happens.
    (document_rows > 1) AS duplicate_rows
FROM lodgements;

COMMENT ON VIEW detection_feed_coverage IS
  'One row per (entity, lodgement minute) across all detection feeds. '
  'Answers "which feed saw this announcement" and "is any lodgement held '
  'twice". Derived and disposable; documents remains the fact.';
