-- Where a targeted ASX retrieval is allowed to point.
--
-- The access decision was amended on 20 August 2026, on the owner's legal
-- advice: retrieving a specific announcement document from asx.com.au is
-- permitted; scraping is not. That distinction only means anything if the
-- code can tell the two apart, and the difference is whether the URL was
-- learned from an announcement we ALREADY know exists.
--
-- This column is that provenance. A URL here says: we detected this
-- announcement from an alert, and this is where its document lives. The
-- fetcher will retrieve nothing else — not a listing, not a search result,
-- not a link found on a page.
--
-- It is deliberately NOT derived from asx_announcement_id by a formula.
-- Guessing a URL pattern from a remembered format would be the platform
-- constructing addresses the ASX never gave it, which is discovery wearing
-- retrieval's clothes. Populate it from a source that states it.
ALTER TABLE documents ADD COLUMN asx_document_url TEXT;

COMMENT ON COLUMN documents.asx_document_url IS
  'Document URL for a specific announcement already detected. The only URL '
  'automated code may retrieve from a restricted host. Never constructed by '
  'pattern — recorded from a source that states it.';
