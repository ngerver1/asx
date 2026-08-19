# alerts/

Raw alert emails, gzipped, committed by the Apps Script in
`tools/apps-script/`. Laid out as `alerts/YYYY/MM/<utc-stamp>-<gmail-id>.eml.gz`.

Append-only. These are the publisher's bytes exactly as sent — this directory
is the raw zone for the detection feed (SPEC §3), so nothing here is edited or
regenerated. Ingest with:

    asx detect --from-dir alerts

Re-running is free: detections are keyed on the ASX announcement number, so
the same alert ingested twice is one row.
