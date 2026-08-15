"""Text extraction from raw document bytes."""

from __future__ import annotations

import io


def pdf_to_text(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def document_text(content: bytes) -> str:
    """Best-effort text for the text-based extraction pass. PDFs go through
    pypdf; anything else is treated as UTF-8 text."""
    if content[:5] == b"%PDF-":
        return pdf_to_text(content)
    return content.decode("utf-8", errors="replace")
