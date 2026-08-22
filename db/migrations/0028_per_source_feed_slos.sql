-- A feed can die while the monitor stays green.
--
-- feed_slos has no source dimension: check_freshness_and_volume filters on
-- doc_class and nothing else, so 'detections_all' counts documents from every
-- source together. Two feeds now run in parallel by decision (0026), and the
-- watchlist-bounded Market Index feed produces enough rows on its own to hold
-- that count above its minimum of 20. The whole-exchange feed could therefore
-- stop returning anything -- a renamed tool argument, a keyword filter that
-- stops matching -- and every daily run would report success for weeks.
--
-- CLAUDE.md is unambiguous about this class of failure: "Zero lodgements in a
-- period is a pipeline alarm until a human says otherwise." It could not be,
-- because nothing could see one feed's silence behind the other's noise.
ALTER TABLE feed_slos ADD COLUMN IF NOT EXISTS detection_source TEXT;

COMMENT ON COLUMN feed_slos.detection_source IS
  'Scope this SLO to one detection feed. NULL means every source together, '
  'which is the right shape for "the platform stopped detecting anything" '
  'and the wrong shape for "one of two feeds stopped".';

-- WHY THESE ROWS DO NOT MAKE THE MONITOR RED TODAY.
--
-- investorpa has never run: the OAuth grant does not exist yet, so the feed
-- has produced zero documents and always will until someone completes the
-- consent. A zero-volume alarm on it would fire from the moment this
-- migration applies until that day, and a monitor that is always red is a
-- monitor nobody reads -- which would recreate the very problem these rows
-- exist to solve, with extra steps.
--
-- The check therefore treats a feed that has NEVER delivered a document as
-- unstarted rather than broken. Silence only becomes an alarm once a feed has
-- shown it can speak. That is self-activating: the first successful run arms
-- its own alarm, and no human has to remember to flip anything.
INSERT INTO feed_slos (feed_name, doc_class, detection_source,
                       max_staleness_hours, min_docs_per_window, window_days,
                       time_column, note)
VALUES
  ('detections_market_index', NULL, 'market_index_alert', 96, 5, 7, 'detected_at',
   'Watchlist-bounded at ~200 codes, so volume is low and lumpy; the floor is '
   'deliberately generous. Silence for a week means the Apps Script trigger, '
   'the Gmail grant or the alert subscription has broken.'),
  ('detections_investorpa', NULL, 'investorpa', 96, 20, 7, 'detected_at',
   'Whole-exchange search: ~15-20 director-interest notices a day, so a week '
   'below 20 means the search stopped matching, not that the market was '
   'quiet. Inert until the feed first delivers -- see the migration comment.')
ON CONFLICT (feed_name) DO NOTHING;
