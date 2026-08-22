-- Rebuild detection_feed_coverage. The first version was wrong twice, in
-- opposite directions, and both errors landed on the number it exists to
-- produce.
--
-- It grouped on (entity_id, date_trunc('minute', lodged_at)). That is the
-- wrong grain in both directions:
--
--   TOO COARSE. Two DIFFERENT directors of one company lodging at 11:59:01
--   and 11:59:02 collapsed into a single row flagged duplicate_rows. Those
--   are two genuine announcements that must both be parsed -- and a company
--   whose directors file together is precisely the batch-lodgement pattern
--   the cluster-buy screen exists to detect. The view cried wolf on the
--   platform's best signal.
--
--   TOO FINE. Market Index reports lodgement to the MINUTE ("Published:
--   20/08/26, 11:59am") and investorpa to the SECOND. One lodgement seen by
--   both feeds could therefore straddle a minute boundary and split into a
--   market_index_only row plus an investorpa_only row -- inflating the one
--   bucket the daily workflow reads.
--
-- And it filtered entity_id IS NOT NULL, silently dropping every detection
-- whose ticker did not resolve. Those are the rows MOST likely to be a
-- coverage gap, and dropping them made the view report perfect agreement
-- while the interesting rows were invisible. Demonstrated rather than
-- argued: in a database whose listings table has not been loaded, all 126
-- Market Index detections have entity_id NULL, so the old view returned
-- nothing whatsoever and looked like a clean bill of health.
--
-- The shape now: ONE ROW PER DETECTION, never a merge. Whether a lodgement
-- was seen by the other feed is asked as a question about that row, not
-- imposed by a GROUP BY, so two directors filing in the same second stay two
-- rows and one lodgement seen twice is two rows that both say 'both'.
DROP VIEW IF EXISTS detection_feed_coverage;

CREATE VIEW detection_feed_coverage AS
SELECT
    d.doc_id,
    d.entity_id,
    d.ticker_as_lodged,
    d.doc_class,
    d.lodged_at,
    d.detection_source,
    d.parse_status,
    CASE
        -- Surfaced as its own bucket rather than excluded. An announcement we
        -- cannot attribute to an entity is a coverage question, not a row to
        -- hide: "prefer a smaller correct dataset with coverage flags over a
        -- complete-looking one" (CLAUDE.md).
        WHEN d.entity_id IS NULL THEN 'unresolved_entity'
        WHEN EXISTS (
            SELECT 1
              FROM documents o
             WHERE o.entity_id = d.entity_id
               AND o.doc_id <> d.doc_id
               AND o.detection_source IS DISTINCT FROM d.detection_source
               AND o.detection_source IN ('market_index_alert', 'investorpa')
               AND o.doc_class IS NOT DISTINCT FROM d.doc_class
               -- The tolerance exists because the two feeds report the same
               -- instant at different precisions: minute versus second, so
               -- the same lodgement can differ by up to 59 seconds. 90s is
               -- that bound plus margin.
               --
               -- UNCALIBRATED, and stated so rather than presented as a
               -- measurement: no lodgement has yet been observed by both
               -- feeds, because investorpa has never run. The residual
               -- ambiguity is real -- two directors of one company filing
               -- within 90 seconds, one seen by each feed, would pair
               -- wrongly. Revisit against the first real overlap; that is
               -- what a calibration is for.
               AND o.lodged_at BETWEEN d.lodged_at - INTERVAL '90 seconds'
                                   AND d.lodged_at + INTERVAL '90 seconds'
        ) THEN 'both'
        WHEN d.detection_source = 'investorpa' THEN 'investorpa_only'
        WHEN d.detection_source = 'market_index_alert' THEN 'market_index_only'
        ELSE 'other_source'
    END AS coverage
FROM documents d
WHERE d.lodged_at IS NOT NULL
  AND d.detection_source IN ('market_index_alert', 'investorpa');

COMMENT ON VIEW detection_feed_coverage IS
  'One row per detection, never a merge. Answers "did the other feed see '
  'this announcement too", which is the question that tells us whether the '
  'whole-exchange feed is really the superset it is assumed to be. A '
  'non-empty market_index_only bucket says it is not. Derived and '
  'disposable; documents remains the fact.';
