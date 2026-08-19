-- Distinguish documents retrieved from the ASX from documents a human opened.
--
-- Under the amended access decision both routes are legitimate, but they are
-- not the same fact, and an audit of "what did automated code fetch from the
-- ASX" must be answerable from the data rather than from memory. Folding
-- targeted retrieval into 'manual_capture' would make the platform unable to
-- show which of its documents it requested itself — exactly the question the
-- amendment invites someone to ask.
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_possession_source_check;
ALTER TABLE documents ADD CONSTRAINT documents_possession_source_check
  CHECK (possession_source = ANY (ARRAY[
    'ir_website',        -- fetched from the company's own site
    'asx_targeted',      -- retrieved from the ASX, specific known document
    'email_attachment',  -- the issuer sent it to us
    'manual_capture',    -- the owner opened it personally
    'filedrop',
    'reference_download'
  ]));

COMMENT ON COLUMN documents.possession_source IS
  'How the bytes were obtained. asx_targeted means automated retrieval of a '
  'specific announcement already detected — permitted since the 20 Aug 2026 '
  'amendment, and deliberately separable from manual_capture so the question '
  '"what did the platform fetch from the ASX" has an answer in the data.';
