# captures/

Announcement PDFs, committed by `forwardAttachments()` in
`tools/apps-script/`, laid out as `captures/YYYY/MM/<date>-<msgid>-<name>.pdf`.

These arrive as **email attachments** from company investor-relations lists.
That matters: nothing here was fetched. asx.com.au is off limits to any
automated device under the access decision, and Market Index's terms cannot
be verified from the platform's network, so no page is ever requested. A
company that emails you its own announcement has sent you the document, which
is possession route 1 with the fetching removed.

Ingest with:

    asx capture --capture-dir captures

Attachments carry no ASX announcement number, so matching falls back to ticker
and lodgement date. Where you know the number, putting it in the filename
gives an exact match instead.
