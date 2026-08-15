-- Derived zone (disposable, rebuilt on schedule), the share-count replay
-- function, signal tables, monitoring config, and the hypothesis log
-- (SPEC §3, §5.4, §7, §8, §12, §13).

-- Bitemporal share-count replay (Invariant 5). Point-in-time correct:
-- p_as_known_at limits the replay to events a market participant could have
-- known about at that moment; null means "use everything we know today".
-- Consolidation/split ratios multiply exactly; fractional-entitlement rounding
-- by issuers is absorbed by the reconciliation tolerance (SPEC §5.4).
CREATE OR REPLACE FUNCTION shares_outstanding(
  p_entity_id   BIGINT,
  p_class_code  TEXT,
  p_as_of       DATE,
  p_as_known_at TIMESTAMPTZ DEFAULT NULL
) RETURNS NUMERIC
LANGUAGE plpgsql STABLE AS $$
DECLARE
  v_qty         NUMERIC;
  v_anchor_date DATE;
  r             RECORD;
BEGIN
  SELECT qty, anchor_date INTO v_qty, v_anchor_date
  FROM share_anchors
  WHERE entity_id = p_entity_id
    AND class_code = p_class_code
    AND anchor_date <= p_as_of
    AND (p_as_known_at IS NULL OR knowable_at <= p_as_known_at)
  ORDER BY anchor_date DESC
  LIMIT 1;

  IF v_qty IS NULL THEN
    RETURN NULL;  -- no anchored opening balance: replay is undefined, not zero
  END IF;

  FOR r IN
    SELECT event_kind, qty_delta, ratio_num, ratio_den
    FROM share_events
    WHERE entity_id = p_entity_id
      AND class_code = p_class_code
      AND event_date > v_anchor_date
      AND event_date <= p_as_of
      AND event_kind <> 'issue_proposed'  -- 3B proposals anticipate, never count (SPEC §5.4)
      AND (p_as_known_at IS NULL OR knowable_at <= p_as_known_at)
    ORDER BY event_date, event_id
  LOOP
    IF r.event_kind IN ('consolidation','split') THEN
      v_qty := v_qty * r.ratio_num / r.ratio_den;
    ELSIF r.qty_delta IS NOT NULL THEN
      v_qty := v_qty + r.qty_delta;
    END IF;
  END LOOP;

  RETURN v_qty;
END;
$$;

-- True-float series (SPEC §8). Derived and disposable; every estimate column's
-- provenance is recorded in coverage_flags — a float estimate without stated
-- coverage is prohibited output.
CREATE TABLE float_series (
  entity_id              BIGINT NOT NULL REFERENCES entities,
  as_of                  DATE   NOT NULL,
  shares_quoted          NUMERIC,
  shares_restricted      NUMERIC,
  escrowed_voluntary_est NUMERIC,
  strategic_holdings_est NUMERIC,
  free_float_est         NUMERIC,
  coverage_flags         TEXT[] NOT NULL,   -- e.g. {mandatory_only,no_substantial_data}
  built_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (entity_id, as_of)
);

-- Derived signal tables carry signal_version (SPEC §7); definitions live in
-- code, versioned.
CREATE TABLE signal_cluster_buys (
  signal_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  signal_version INT    NOT NULL,
  entity_id      BIGINT NOT NULL REFERENCES entities,
  window_start   DATE   NOT NULL,
  window_end     DATE   NOT NULL,
  n_directors    INT    NOT NULL,
  total_consideration_aud NUMERIC,
  -- actionable no earlier than the latest lodgement in the cluster (Invariant 2)
  knowable_at    TIMESTAMPTZ NOT NULL,
  trade_ids      BIGINT[] NOT NULL,        -- provenance (Invariant 12)
  built_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Freshness / volume SLOs per feed (Invariant 7): zero lodgements is a
-- probable pipeline failure until a human confirms otherwise.
CREATE TABLE feed_slos (
  feed_name            TEXT PRIMARY KEY,
  doc_class            TEXT,          -- null = all documents from the feed
  max_staleness_hours  INT  NOT NULL,
  min_docs_per_window  INT  NOT NULL,
  window_days          INT  NOT NULL,
  note                 TEXT
);

INSERT INTO feed_slos (feed_name, doc_class, max_staleness_hours, min_docs_per_window, window_days, note) VALUES
  ('announcements_all', NULL,     30, 50, 5, 'Any announcement; near-zero weeks outside holidays are alarms'),
  ('app_3y',            'app_3y', 96,  5, 7, '3Y volume is seasonal (results/AGM spikes) but never zero for weeks');

-- Alarm log written by the monitor so silence itself is detectable
-- (a monitor that stops running shows up as a gap in monitor_runs).
CREATE TABLE monitor_runs (
  run_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ran_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  ok         BOOLEAN NOT NULL,
  alarms     JSONB NOT NULL DEFAULT '[]'
);

-- Multiple-testing discipline (SPEC §12): a registered log of every hypothesis
-- tested, so survivors can be judged against the number of draws.
CREATE TABLE hypothesis_log (
  hypothesis_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  description   TEXT NOT NULL,
  signal_name   TEXT,
  signal_version INT,
  outcome       TEXT,          -- filled after the study: 'positive','negative','inconclusive'
  note          TEXT
);
