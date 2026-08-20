-- "Direct and indirect" is a category, not an ambiguity.
--
-- 58 of the 209 Appendix 3Y forms in the captured corpus (28%) state the
-- director's interest as BOTH direct and indirect — a director who holds
-- some shares personally and some through a family trust or super fund, and
-- whose notice covers movements in both. The form says so plainly.
--
-- Without this value those 58 forms would be recorded as 'unknown', which
-- would be wrong twice over: it discards a fact the document states, and it
-- puts a quarter of the corpus in the bucket reserved for things we could
-- not read. Invariant 8 forbids blending categories and forbids substantive
-- defaults; it does not require collapsing a real third category into
-- 'unknown'.
ALTER TABLE director_trades DROP CONSTRAINT IF EXISTS director_trades_interest_nature_check;
ALTER TABLE director_trades ADD CONSTRAINT director_trades_interest_nature_check
  CHECK (interest_nature IN ('direct', 'indirect', 'both', 'unknown'));
