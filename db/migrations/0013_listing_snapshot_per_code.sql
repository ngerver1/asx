-- A listing snapshot is per security code, not per entity.
--
-- listing_snapshots was keyed (entity_id, as_at), which cannot represent a
-- dual-class issuer: NWS and NWSLV, AUQ and AUQN, CHR and CHRCB, EMU and
-- EMUCA are one entity with two codes on the same date. The loader's
-- ON CONFLICT (entity_id, as_at) DO UPDATE therefore merged each pair into
-- one row carrying the first code's ticker and the second code's figures, and
-- the other four codes silently had no snapshot at all — reading on a size
-- screen as "market cap unknown" rather than as their real value.
--
-- The publisher's file has one row per code and so does this table now.
ALTER TABLE listing_snapshots DROP CONSTRAINT listing_snapshots_pkey;
ALTER TABLE listing_snapshots ADD PRIMARY KEY (entity_id, as_at, ticker);

COMMENT ON TABLE listing_snapshots IS
  'One row per listed security code per publisher extract date. Dual-class '
  'issuers legitimately have several rows for one entity on one date.';
