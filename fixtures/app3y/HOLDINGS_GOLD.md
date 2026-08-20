# holdings_gold.jsonl

Ten real Appendix 3Y lodgements, captured verbatim from the corpus, for the
question "what did this director hold before and after, in the class that
actually changed?"

Each case carries the form's own cells — `held_before`, `held_after`,
`security_class`, `qty_acquired`, `qty_disposed`, `interest_nature` — exactly
as the reader extracts them, so the fixture is self-contained and the test
does not need a database.

`expect` is the answer **read off the form by hand**, from the arithmetic the
issuer printed: held after = held before + acquired − disposed. It was not
produced by running the parser and recording its output, which would only pin
the behaviour in place rather than test it.

`expect: null` means the case **must return nothing**. Those two are here
deliberately and are the more important half: doc 830's class cell is a
garbled multi-class blob, and doc 1362's before-cell states a parcel that
reconciles with nothing on the form. A reader that produces a number for
either is guessing, and Invariant 8 says an unattributable holding is
`unknown`.

`klass` records which class the change was in — `ordinary` or `other` — because
a third of the corpus reports a change in options or performance rights while
the ordinary holding sits unchanged beside it.
