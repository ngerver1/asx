"""Registry of implemented parsers. The ingestion pipeline derives the set of
parseable document classes from here, so a class only counts as parseable
once a parser actually exists for it — anything else would strand documents
in 'unparsed' and permanently saturate the stuck-document alarm."""

from __future__ import annotations

from asx.parse.app3y import App3YParser

PARSERS = {
    "app3y": App3YParser,
}


def parseable_doc_classes() -> set[str]:
    classes: set[str] = set()
    for cls in PARSERS.values():
        classes |= cls.doc_classes
    return classes
