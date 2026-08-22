"""Per-feed silence detection.

Two detection feeds now run in parallel. The SLO table had no source
dimension, so `detections_all` counted them together and the watchlist-bounded
Market Index feed produced enough rows on its own to hold the total above its
minimum. The whole-exchange feed could stop returning anything and every daily
run would still report success — the failure CLAUDE.md names outright ("zero
lodgements in a period is a pipeline alarm until a human says otherwise"),
made invisible by the arithmetic.

These tests assert on alarm KIND rather than on "no alarms fired". The
`conn` fixture deliberately does not truncate `feed_slos`, so the pre-existing
global SLOs are live in every one of them and a bare `assert not alarms` would
be asserting something else entirely.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from asx.monitor.checks import check_freshness_and_volume

NOW = datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc)


def _detection(conn, *, source: str, detected_at: datetime, ticker="AAA"):
    """A bare detection row: known to exist, no bytes held."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
                 (source, source_ref, ticker_as_lodged, title, detection_source,
                  detected_at, detection_key, parse_status)
               VALUES ('test', %s, %s, 'Change of Director''s Interest Notice',
                       %s, %s, %s, 'detected')""",
            (f"ref-{source}-{detected_at.isoformat()}-{ticker}", ticker,
             source, detected_at, f"key-{source}-{detected_at.isoformat()}-{ticker}"),
        )
    conn.commit()


def _alarms_for(conn, feed_name: str, now: datetime = NOW) -> list:
    return [a for a in check_freshness_and_volume(conn, now)
            if a.detail.startswith(feed_name)]


def test_a_feed_that_has_never_delivered_is_unstarted_not_broken(conn):
    """investorpa cannot run until someone completes an OAuth consent. Alarming
    on it every day until then would make the monitor permanently red, and a
    monitor nobody reads recreates the problem these SLOs exist to solve."""
    assert _alarms_for(conn, "detections_investorpa") == []


def test_the_first_delivery_arms_the_alarm(conn):
    """Self-activating: silence becomes an alarm once a feed has shown it can
    speak, so nobody has to remember to switch anything on."""
    # Delivered once, long ago — outside the 7-day window.
    _detection(conn, source="investorpa", detected_at=NOW - timedelta(days=30))
    alarms = _alarms_for(conn, "detections_investorpa")
    assert [a.check for a in alarms] == ["volume"]
    assert "ZERO documents" in alarms[0].detail


def test_a_live_feed_within_its_baseline_does_not_alarm(conn):
    for i in range(25):
        _detection(conn, source="investorpa",
                   detected_at=NOW - timedelta(hours=i), ticker=f"A{i:02d}")
    assert _alarms_for(conn, "detections_investorpa") == []


def test_one_feeds_noise_cannot_hide_the_others_silence(conn):
    """The actual bug. Market Index alone clears the global `detections_all`
    minimum of 20, so before this the whole-exchange feed could be dead and
    nothing would say so."""
    for i in range(30):
        _detection(conn, source="market_index_alert",
                   detected_at=NOW - timedelta(hours=i), ticker=f"M{i:02d}")
    # investorpa spoke once, a month ago, and has said nothing since.
    _detection(conn, source="investorpa", detected_at=NOW - timedelta(days=30))

    everything = check_freshness_and_volume(conn, NOW)
    # The global SLO is satisfied by Market Index's volume...
    assert not [a for a in everything if a.detail.startswith("detections_all")]
    # ...and the per-source SLO still catches the dead feed behind it.
    assert [a.detail for a in everything
            if a.detail.startswith("detections_investorpa")], (
        "a dead whole-exchange feed reported green behind the other feed's "
        "volume, which is the failure this SLO exists to prevent")


def test_a_thin_week_is_reported_as_thin_not_as_silence(conn):
    """Below baseline and zero are different operational facts and must not
    collapse into one message."""
    for i in range(3):
        _detection(conn, source="investorpa",
                   detected_at=NOW - timedelta(hours=i), ticker=f"B{i:02d}")
    alarms = _alarms_for(conn, "detections_investorpa")
    assert [a.check for a in alarms] == ["volume"]
    assert "below baseline" in alarms[0].detail
    assert "ZERO" not in alarms[0].detail
