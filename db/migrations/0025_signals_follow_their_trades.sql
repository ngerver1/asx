-- Reprocessing must not be blocked by derived data.
--
-- `make reprocess` is the ONLY sanctioned path for fixing a systematic parse
-- error: canonical tables are never hand-edited, so a corrected reading is
-- applied by replacing the rows a document produced. `apply_trades` does that
-- with DELETE-then-INSERT on director_trades.
--
-- signal_conviction_buys references those trades, and its foreign key was
-- restricting. So the first reprocess after any signal build died on:
--
--   ForeignKeyViolation: update or delete on table "director_trades" violates
--   foreign key constraint "signal_conviction_buys_trade_id_fkey"
--
-- which means the recovery path for a bad parser was disabled by the very
-- act of building a screen from it — and disabled loudly only at doc 4 of
-- 859, after three documents had already been rewritten.
--
-- A signal is derived-zone data whose whole contract is that it is rebuilt,
-- never patched (SPEC §3). It exists only while the trade it was computed
-- from exists, so CASCADE states that relationship correctly: replace the
-- trade and the stale signal row goes with it, to be recreated by
-- `asx build-signals`. The alternative — a signal row pointing at a trade_id
-- that no longer exists — is the outcome the foreign key is there to prevent,
-- and is not what RESTRICT was buying.
--
-- Operational consequence, stated plainly: `asx reprocess --apply` can now
-- remove signal rows. Rebuild after reprocessing, always:
--
--     asx reprocess --parser=app3y --apply && asx build-signals
ALTER TABLE signal_conviction_buys
  DROP CONSTRAINT signal_conviction_buys_trade_id_fkey;

ALTER TABLE signal_conviction_buys
  ADD CONSTRAINT signal_conviction_buys_trade_id_fkey
  FOREIGN KEY (trade_id) REFERENCES director_trades (trade_id) ON DELETE CASCADE;
