"""The amended ASX access rule, enforced in code.

The access decision changed on 20 Aug 2026: retrieving a specific announcement
document from asx.com.au is permitted, scraping is not. That distinction is
worth nothing unless the code can tell the two apart, so these tests pin the
boundary from both directions — what is now allowed, and what stays refused
no matter how the caller asks.
"""

from __future__ import annotations

import pytest

from asx.ingest import fetch_guard as fg
from asx.ingest.fetch_guard import ProhibitedSourceError, assert_fetchable

PDF = "https://announcements.asx.com.au/asxpdf/20260819/pdf/2a1690214.pdf"


@pytest.fixture(autouse=True)
def _fresh_budget():
    fg.reset_restricted_budget()
    yield
    fg.reset_restricted_budget()


def test_a_targeted_document_is_now_allowed():
    assert_fetchable(PDF, targeted_document=True)


def test_the_same_url_is_refused_without_the_targeted_assertion():
    """The default is still no. A caller that has not read the URL off a
    detection it already holds has no business fetching it."""
    with pytest.raises(ProhibitedSourceError, match="targeted-retrieval only"):
        assert_fetchable(PDF)


def test_a_page_is_refused_even_when_targeted():
    """Retrieval is for documents. Fetching ASX pages is how targeted
    retrieval turns into scraping."""
    with pytest.raises(ProhibitedSourceError, match="addresses a page"):
        assert_fetchable("https://www.asx.com.au/markets/company/AXP",
                         targeted_document=True)


@pytest.mark.parametrize("url", [
    "https://www.asx.com.au/asx/statistics/announcements.do?by=asxCode&page=2",
    "https://www.asx.com.au/search?q=appendix+3y",
    "https://announcements.asx.com.au/browse/2026/08",
    "https://example.com/list?query=director+interest",
])
def test_discovery_endpoints_are_refused_on_any_host_and_any_flag(url):
    """No caller flag turns a search result into a specific known document,
    and the rule is not ASX-specific: discovery is discovery."""
    with pytest.raises(ProhibitedSourceError, match="search, listing or browse"):
        assert_fetchable(url, targeted_document=True)


def test_a_run_cannot_become_a_crawl():
    """A bounded run is the difference between retrieval and a crawl, so the
    ceiling is enforced rather than assumed."""
    for i in range(fg.MAX_RESTRICTED_FETCHES_PER_RUN):
        assert_fetchable(f"https://announcements.asx.com.au/a/{i}.pdf",
                         targeted_document=True)
    with pytest.raises(ProhibitedSourceError, match="per-run ceiling"):
        assert_fetchable(PDF, targeted_document=True)
    fg.reset_restricted_budget()
    assert_fetchable(PDF, targeted_document=True)


def test_an_unrestricted_host_still_needs_a_basis_but_not_targeting():
    """"Not the ASX" was never the same as "fetch freely". An ordinary host
    needs a recorded terms basis, and then needs nothing further — no
    targeted_document assertion, because the targeted gate exists for the
    restricted host specifically."""
    with pytest.raises(ProhibitedSourceError):
        assert_fetchable("https://example.com.au/investors/3y.pdf")
    assert_fetchable("https://example.com.au/investors/3y.pdf",
                     terms_basis="owner spot-checked this IR site")


def test_only_the_possession_path_asserts_targeting():
    """`targeted_document=True` must be passed from exactly one call site.

    The assertion means "I read this URL off a detection I already hold". If
    it spreads to a second caller it stops meaning that, and the boundary
    between retrieval and scraping becomes a matter of intention again. The
    guard module itself is excluded: it defines the parameter and documents
    it, rather than asserting it.
    """
    import pathlib
    import re

    src = pathlib.Path(__file__).parent.parent / "src" / "asx"
    call = re.compile(r"(fetch|assert_fetchable)\s*\([^)]*targeted_document\s*=\s*True")
    hits = []
    for path in sorted(src.rglob("*.py")):
        if path.name == "fetch_guard.py":
            continue
        text = path.read_text()
        for match in call.finditer(text):
            hits.append(f"{path.relative_to(src)}:{text[:match.start()].count(chr(10)) + 1}")
    assert len(hits) == 1, f"targeted_document=True is asserted in {hits}"
    assert hits[0].startswith("ingest/possession.py:"), hits


# --- a source must have a recorded terms basis before it is fetched -------

def test_an_undeclared_source_is_refused_however_useful_it_looks():
    """Prompted by a suggestion to source PDFs from hotcopper.com.au. It is a
    third-party forum with its own terms, which the ASX legal advice does not
    cover and which cannot be read from this network. Invariant 11 wants the
    basis known BEFORE the fetch, not inferred from the source being handy."""
    with pytest.raises(ProhibitedSourceError, match="no terms basis is recorded"):
        assert_fetchable("https://hotcopper.com.au/documents/2a1690214.pdf")
    with pytest.raises(ProhibitedSourceError, match="no terms basis is recorded"):
        assert_fetchable("https://example.org/investors/3y.pdf")


def test_a_caller_may_state_a_standing_basis():
    """Company IR sites are spot-checked per company, so the IR path carries
    the basis rather than every site being pre-declared."""
    assert_fetchable("https://xyzlimited.com.au/investors/3y-aug26.pdf",
                     terms_basis="access decision §6: owner spot-checks IR terms")


def test_a_terms_basis_does_not_unlock_discovery_or_the_asx():
    """The basis says 'we may fetch this host'. It never says 'and therefore
    anything on it'."""
    with pytest.raises(ProhibitedSourceError, match="search, listing or browse"):
        assert_fetchable("https://hotcopper.com.au/asx/bca/?page=2",
                         terms_basis="stated")
    with pytest.raises(ProhibitedSourceError, match="targeted-retrieval only"):
        assert_fetchable("https://announcements.asx.com.au/x.pdf",
                         terms_basis="stated")
