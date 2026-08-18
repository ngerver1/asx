-- Foreign issuers carry an ARBN, not an ACN.
--
-- 117 of the companies in the ASX listed file resolve to ASIC registrations of
-- type FNOS — "Foreign company (a company incorporated outside Australia but
-- registered as a foreign company in Australia)". Those bodies are issued an
-- Australian Registered Body Number, which is also nine digits and therefore
-- fitted silently into entities.acn. Same shape, different identifier: an ARBN
-- is not an ACN, and storing one in the other's column makes acceptance
-- criterion 0.2 ("resolved ACN or explicit foreign flag") pass on a
-- misstatement.
--
-- Foreign registrations now populate `arbn` and set entity_kind='foreign',
-- which is the explicit flag the criterion actually asks for.
ALTER TABLE entities ADD COLUMN arbn CHAR(9) UNIQUE;

COMMENT ON COLUMN entities.arbn IS
  'Australian Registered Body Number, for bodies registered with ASIC that do '
  'not have an ACN (registered foreign companies and registered Australian '
  'bodies). Never an ACN — the two are separate registers that happen to share '
  'a nine-digit format.';
COMMENT ON COLUMN entities.acn IS
  'Australian Company Number. NULL for foreign issuers: see arbn.';

-- An entity may carry one registration number or the other, never both.
ALTER TABLE entities ADD CONSTRAINT entities_one_registration_number
  CHECK (acn IS NULL OR arbn IS NULL);
