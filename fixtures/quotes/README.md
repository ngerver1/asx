# Quote-page fixtures

`stockanalysis_asx_tne.html` is a verbatim capture of
`https://stockanalysis.com/quote/asx/TNE/`, retrieved 20 August 2026 under
the platform's declared user-agent.

It is here so the quote parser is tested against the page shape the source
actually serves, rather than a hand-written approximation of it — the same
reason the Appendix 3Y gold set holds real notices. When the source changes
its layout the parser should fail loudly on this file, which is the signal to
re-capture it and re-read the parse, not to loosen the parser until it passes.

Kept for testing only, not republished: the terms permit unmodified,
attributed snippets, and a test fixture in a private repository is neither
republication nor redistribution.
