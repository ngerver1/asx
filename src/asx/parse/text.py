"""Text extraction from raw document bytes."""

from __future__ import annotations

import io


class UnreadableDocument(RuntimeError):
    """The environment cannot read this document — not the document's fault.

    Kept distinct from "this PDF has no extractable text" because the two
    demand opposite responses. A scanned page is a document problem: route it
    to review and carry on. A missing decryption backend is an operator
    problem affecting EVERY encrypted document at once, and carrying on
    produces a complete-looking dataset built from almost nothing.
    """


def pdf_pages(content: bytes):
    """The pages of a PDF, or UnreadableDocument if the environment can't.

    55 of the 60 real ASX announcement PDFs captured so far are AES-encrypted
    with an empty user password. pypdf opens those silently when a crypto
    backend is installed and raises DependencyError when one is not — so an
    install missing `pypdf[crypto]` reads 5 documents in 60. Every caller
    below catches broad exceptions and degrades gracefully, which is right for
    a malformed file and catastrophic for a missing library: the pipeline
    would keep running, keep reporting successes, and hold nothing.
    """
    from pypdf import PdfReader
    from pypdf.errors import DependencyError

    try:
        return PdfReader(io.BytesIO(content)).pages
    except DependencyError as exc:
        raise UnreadableDocument(
            f"This PDF is encrypted and no decryption backend is installed "
            f"({exc}). Install the platform's declared dependencies — "
            f"pip install -e '.[dev]' — which include pypdf[crypto]. Most "
            f"real ASX announcement PDFs are encrypted, so without it the "
            f"platform reads almost nothing and reports no error."
        ) from exc


def pdf_to_text(content: bytes) -> str:
    return "\n\n".join((page.extract_text() or "") for page in pdf_pages(content))


def document_text(content: bytes) -> str:
    """Best-effort text for the text-based extraction pass. PDFs go through
    pypdf; anything else is treated as UTF-8 text."""
    if content[:5] == b"%PDF-":
        return pdf_to_text(content)
    return content.decode("utf-8", errors="replace")
