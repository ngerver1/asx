-- Display quotes: what a share is worth now, beside what the director paid.
--
-- ACCESS_DECISION §3 says there is no price vendor, and that stands. This is
-- not one. The distinction is the whole reason this table is separate from
-- anything the backtester could reach:
--
--   a BACKTEST price source must be survivorship-complete (Invariant 4) —
--   it has to price the companies that later delisted, or every study run
--   over it flatters itself. No free source does that.
--
--   a DISPLAY quote answers a narrower question — "what is this trading at
--   today?" — for a name that is listed today, on a screen a human reads.
--   A delisted name simply has no answer, and saying so is a correct output.
--
-- So: quotes land here, `asx/backtest/harness.py` never learns this table
-- exists, and nothing in this file implements the `PriceSource` protocol.
-- Registering a display source as a price source is how a backtest starts
-- lying, and it is a one-line mistake, so the separation is structural.
--
-- Owner sign-off, 20 Aug 2026: stockanalysis.com declared as a display-only
-- quote source under Invariant 11. Basis recorded in
-- fetch_guard.DECLARED_SOURCES and in ACCESS_DECISION §3.
CREATE TABLE price_quotes (
  quote_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  -- Never join on ticker (Invariant 1). The quote is ADDRESSED by ticker,
  -- because that is the only handle a price site has, but it is STORED
  -- against the entity the ticker resolved to at retrieval time.
  entity_id      BIGINT NOT NULL REFERENCES entities,
  -- The alias actually used, kept so a wrong resolution is auditable rather
  -- than invisible: tickers are recycled, and "which code did we ask for?"
  -- must be answerable from the row.
  ticker_used    TEXT   NOT NULL,
  -- What the source said it gave back. Checked against ticker_used before a
  -- price is accepted; a redirect to another security is a wrong answer, not
  -- a near miss.
  symbol_returned TEXT,
  exchange_returned TEXT,

  price          NUMERIC,       -- NULL unless status = 'ok'
  currency       TEXT,
  previous_close NUMERIC,

  -- Invariant 2 in the form it takes for a quote. `as_at` is the moment the
  -- price was struck (the source's own timestamp); `retrieved_at` is when we
  -- asked. They are different facts and both are needed: a screen that shows
  -- a Friday close on a Tuesday is only misleading if it hides the Friday.
  as_at          TIMESTAMPTZ,
  as_at_date     DATE,
  -- The source's own human rendering, verbatim ("Aug 20, 2026, 4:10 PM
  -- AEST"). Kept unparsed as well as parsed so a timezone bug in our parsing
  -- can be caught against what the source actually said.
  as_at_label    TEXT,
  -- stockanalysis.com labels ASX quotes "Delayed Price". A quote that may be
  -- 15+ minutes old is fine for a screen and must not be called "live".
  delayed        BOOLEAN NOT NULL DEFAULT TRUE,
  market_status  TEXT,

  source_name    TEXT   NOT NULL,
  -- Required, not optional: an undated, unattributed number beside figures
  -- traced to specific lodgements is the weakest thing on the page.
  source_url     TEXT   NOT NULL,
  retrieved_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- A failed lookup is recorded, never skipped. Delisted entities stay in
  -- every universe (Invariant 4): the row keeps its place on the screen and
  -- carries the reason its price is blank.
  status         TEXT   NOT NULL CHECK (status IN
                   ('ok','not_found','unparsed','fetch_error')),
  status_detail  TEXT,

  CONSTRAINT price_quotes_ok_has_price CHECK (
    status <> 'ok' OR (price IS NOT NULL AND as_at_date IS NOT NULL)),
  CONSTRAINT price_quotes_price_positive CHECK (price IS NULL OR price > 0)
);

-- Every retrieval is kept rather than overwritten, so "what did the screen
-- say last Tuesday, and where did that number come from?" stays answerable.
-- Readers take the newest row per entity.
CREATE INDEX price_quotes_entity_idx ON price_quotes (entity_id, retrieved_at DESC);
