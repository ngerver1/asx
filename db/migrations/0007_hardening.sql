-- Hardening from the invariant review:
--
-- 1. share_anchors provenance (Invariant 12): every replay starts from an
--    anchor, so an anchor must say where it came from. Non-document anchors
--    (vendor extract, manual seed) carry a source enum plus a note.
-- 2. share_events CHECK tightening (Invariant 5/6): the original constraint
--    admitted zero/negative ratios (count -> 0 or sign-flip), inverted
--    consolidation ratios (100x errors), and counting events with NULL
--    qty_delta (silent no-ops). All are parse errors, not data.
--
-- Anchor semantics note (documented, not changed): anchor qty is the count as
-- at the END of anchor_date; events dated exactly on the anchor date are
-- already inside the anchored figure and are not re-applied by
-- shares_outstanding().

ALTER TABLE share_anchors
  ADD COLUMN source TEXT NOT NULL DEFAULT 'document'
    CHECK (source IN ('document', 'vendor', 'manual')),
  ADD COLUMN note TEXT;

ALTER TABLE share_anchors
  ADD CONSTRAINT share_anchors_provenance CHECK (
    (source = 'document' AND source_doc_id IS NOT NULL)
    OR (source IN ('vendor', 'manual') AND note IS NOT NULL)
  );

-- One live persons row per normalised name; find_or_create_person races
-- through ON CONFLICT instead of creating duplicates that would break
-- supersession grouping. Merged (historical) rows are exempt.
CREATE UNIQUE INDEX persons_live_name_norm_uq ON persons (name_norm)
  WHERE merged_into IS NULL;

-- Weekend-aware staleness: 30h fired every Monday morning (Friday close to
-- Monday open is ~64h). 72h still catches a dead feed by Tuesday; proper
-- trading-calendar awareness is a monitoring refinement, not a seed value.
UPDATE feed_slos SET max_staleness_hours = 72 WHERE feed_name = 'announcements_all';

ALTER TABLE share_events DROP CONSTRAINT share_events_check;
ALTER TABLE share_events ADD CONSTRAINT share_events_shape CHECK (
  (
    event_kind IN ('consolidation', 'split')
    AND ratio_num IS NOT NULL AND ratio_den IS NOT NULL
    AND ratio_num > 0 AND ratio_den > 0
    AND qty_delta IS NULL
    -- direction sanity: a consolidation reduces the count, a split increases
    -- it; an inverted ratio is a misparse worth rejecting at the door
    AND (event_kind <> 'consolidation' OR ratio_num < ratio_den)
    AND (event_kind <> 'split' OR ratio_num > ratio_den)
  )
  OR (
    event_kind NOT IN ('consolidation', 'split')
    AND ratio_num IS NULL AND ratio_den IS NULL
    AND qty_delta IS NOT NULL
  )
);
