-- A change that happened over several days, recorded as such (SPEC §7).
--
-- Appendix 3Y gives one "Date of change" box, and a director who transacted
-- more than once writes what actually happened:
--
--     Date of change   12-14 August 2026
--     Date of change   A. 17 August 2026  B. 13 August 2026
--     Date of change   (a) 14 August 2026 (b) and (c) 12 August 2026
--
-- event_date is a single DATE, so parse_date refused all three and 21
-- documents produced nothing. The parser was right to refuse — picking one
-- date out of several invents the fact the cluster-buy window keys on — but
-- refusing was not the only option. The form states its dates; the schema
-- simply could not hold them.
--
-- So it holds them now. event_dates_stated keeps every date the field
-- printed, verbatim and in order (Invariant 9: as-reported is preserved), and
-- event_date_basis says how event_date was arrived at:
--
--   stated               the form gave exactly one date. Unchanged behaviour,
--                        and what almost every row is.
--   range_midpoint       the form gave a continuous period ("12-14 August")
--                        and event_date is its midpoint. The change did occur
--                        inside this span.
--   enumeration_midpoint the form listed discrete dates that could not be
--                        matched to their parcels, and event_date is their
--                        midpoint. WEAKER than a range: the midpoint of
--                        "17 August and 13 August" is 15 August, a day on
--                        which nothing happened. Owner's decision, Aug 2026,
--                        to take the estimate rather than lose the row —
--                        recorded here so any analysis that cannot tolerate
--                        an invented day can exclude it with one predicate.
--
-- Where the form's labels DO map dates to parcels, no estimate is needed and
-- none is made: those become separate rows, each with its own stated date.
--
-- This mirrors documents.lodged_at_source, where 'pdf_creation' marks a
-- timestamp as a proxy rather than the exchange's own. A derived date without
-- a stated basis is the prohibited output; a labelled one is usable.
ALTER TABLE director_trades ADD COLUMN IF NOT EXISTS event_dates_stated DATE[];
ALTER TABLE director_trades ADD COLUMN IF NOT EXISTS event_date_basis TEXT
  NOT NULL DEFAULT 'stated';

ALTER TABLE director_trades DROP CONSTRAINT IF EXISTS director_trades_event_date_basis;
ALTER TABLE director_trades ADD CONSTRAINT director_trades_event_date_basis CHECK (
  event_date_basis IN ('stated', 'range_midpoint', 'enumeration_midpoint')
);

-- An estimated date must say what it was estimated from. A basis other than
-- 'stated' with no dates behind it is an assertion with no source.
ALTER TABLE director_trades DROP CONSTRAINT IF EXISTS director_trades_estimated_date_has_source;
ALTER TABLE director_trades ADD CONSTRAINT director_trades_estimated_date_has_source CHECK (
  event_date_basis = 'stated'
  OR (event_dates_stated IS NOT NULL AND cardinality(event_dates_stated) > 1)
);

-- Screens that cannot tolerate an invented day filter on this.
CREATE INDEX IF NOT EXISTS director_trades_estimated_date_idx
  ON director_trades (event_date_basis) WHERE event_date_basis <> 'stated';
