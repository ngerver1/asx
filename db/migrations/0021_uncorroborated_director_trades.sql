-- Derived zone: director readings that could not be corroborated (SPEC §3).
--
-- Invariant 6 keeps unvalidated extractions out of canonical, and that stays
-- true: nothing here is written to director_trades. But "not canonical" was
-- being read as "not usable", and it is not the same thing. Of 109 documents
-- in the review queue, 82 carry both a date of change and a quantity — more
-- usable readings than the 75 that reached canonical. Discarding them is its
-- own kind of error.
--
-- The reason most of them are here is narrow. The rules reader is single-pass,
-- so its only corroboration is the form's own arithmetic:
--
--     held after = held before + acquired - disposed
--
-- A notice that does not print the before/after figures offers nothing to
-- check against, so the reading is refused. That says the reading is
-- UNVERIFIED, not that it is wrong — and a director acquiring 534,188 shares
-- is worth seeing with a warning attached rather than not at all.
--
-- This is a view, not a table: the derived zone is disposable (SPEC §3), and a
-- view cannot drift from the parsed rows it reads or be left unbuilt.
--
-- Three things every row carries, because a derived estimate without stated
-- coverage is prohibited output (the rule float_series already follows):
--
--   uncorroborated_because  the validator's own findings, verbatim. Not
--                           re-derived here — the parser said why. All three
--                           lists are gathered: the dominant reason
--                           ("arithmetic unverifiable") is recorded as a
--                           warning and a disagreement, never as an error, so
--                           reading errors alone leaves most rows silently
--                           unexplained.
--   corroboration           'unverified' where the form printed nothing to
--                           check the reading against, 'contradicted' where
--                           the arithmetic WAS checked and did not hold. Those
--                           are different facts and must not share a bucket: a
--                           reading nobody could verify may well be right,
--                           whereas one whose own sum comes to 1 against
--                           1,872,884 is known to be misread. Filter on this
--                           before treating a row as signal.
--   knowable_at             from the document's lodgement. Every analytic
--                           joins on it (Invariant 2), so a reading whose
--                           document has no timestamp is excluded entirely.
--   entity_id               resolved. Every join in the platform is on it
--                           (Invariant 1), so an unresolved document is
--                           excluded rather than keyed on a ticker.
--
-- Excluded on purpose: readings with no date of change (no event_date means no
-- fact this platform can carry) and readings with no acquired/disposed
-- quantity (nothing to act on). A malformed date or quantity is treated the
-- same way — the guards below only admit values that are unambiguously one.
-- Dropped and recreated rather than replaced: CREATE OR REPLACE VIEW can only
-- append columns, and the derived zone is disposable by definition (SPEC §3),
-- so there is nothing here to preserve.
DROP VIEW IF EXISTS uncorroborated_director_trades;
CREATE VIEW uncorroborated_director_trades AS
WITH latest AS (
  -- Reprocessing appends a new parser_version rather than overwriting
  -- (Invariant 3), so the newest reading per document is the current one.
  SELECT DISTINCT ON (doc_id, parser_name)
         doc_id, parser_name, parser_version, payload, validation, confidence
    FROM parsed_records
   ORDER BY doc_id, parser_name, parser_version DESC
)
SELECT
    d.doc_id,
    d.entity_id,
    l.parser_name,
    l.parser_version,
    notice->>'director_name'                    AS person_name_raw,
    (notice->>'date_of_change')::date           AS event_date,
    d.lodged_at                                 AS knowable_at,
    d.lodged_at_source,
    security->>'security_class'                 AS security_class,
    NULLIF(security->>'qty_acquired', '')::numeric  AS qty_acquired,
    NULLIF(security->>'qty_disposed', '')::numeric  AS qty_disposed,
    security->>'consideration_text'             AS consideration_text,
    CASE WHEN security->>'consideration_aud' ~ '^-?[0-9]+(\.[0-9]+)?$'
         THEN (security->>'consideration_aud')::numeric END AS consideration_aud,
    notice->>'interest_nature'                  AS interest_nature,
    notice->>'indirect_detail'                  AS indirect_detail,
    l.confidence,
    CASE WHEN EXISTS (SELECT 1 FROM unnest(f.findings) g
                       WHERE g LIKE '%arithmetic: %')
         THEN 'contradicted' ELSE 'unverified' END AS corroboration,
    -- Never empty: a derived row that cannot say why it is unverified is
    -- exactly the prohibited output this column exists to prevent.
    CASE WHEN cardinality(f.findings) > 0 THEN f.findings
         ELSE ARRAY['routed to review with no validator finding recorded']
    END                                         AS uncorroborated_because
  FROM latest l
  JOIN documents d ON d.doc_id = l.doc_id
  CROSS JOIN LATERAL jsonb_array_elements(
         COALESCE(l.payload->'primary'->'notices', '[]'::jsonb)) AS notice
  CROSS JOIN LATERAL jsonb_array_elements(
         COALESCE(notice->'securities', '[]'::jsonb)) AS security
  CROSS JOIN LATERAL (
    SELECT COALESCE(array_agg(DISTINCT finding ORDER BY finding),
                    ARRAY[]::text[]) AS findings
      FROM (
        SELECT jsonb_array_elements_text(
                 COALESCE(l.validation->'errors', '[]'::jsonb)) AS finding
        UNION
        SELECT jsonb_array_elements_text(
                 COALESCE(l.validation->'warnings', '[]'::jsonb))
        UNION
        SELECT jsonb_array_elements_text(
                 COALESCE(l.validation->'disagreements', '[]'::jsonb))
      ) AS all_findings
  ) AS f
 WHERE d.parse_status = 'review'          -- validated readings are in canonical
   AND d.entity_id IS NOT NULL
   AND d.lodged_at IS NOT NULL
   AND notice->>'date_of_change' ~ '^\d{4}-\d{2}-\d{2}$'
   AND (   security->>'qty_acquired' ~ '^-?[0-9]+(\.[0-9]+)?$'
        OR security->>'qty_disposed' ~ '^-?[0-9]+(\.[0-9]+)?$');

COMMENT ON VIEW uncorroborated_director_trades IS
  'Director readings that failed corroboration and are NOT in canonical. '
  'Every row failed corroboration: filter corroboration = ''unverified'' and '
  'read uncorroborated_because before use; ''contradicted'' rows are known '
  'misreads. '
  'Never UNION this with director_trades without carrying that column.';
