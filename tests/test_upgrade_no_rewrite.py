"""Tests for the no-rewrite leap-table architecture and legacy upgrade.

These complement the original suite: leap-table replacement must leave
exposure rows and time indexes exactly as stored, UTC-text starts keep their
UTC label, real-second starts keep their absolute instant, conflict detection
is index-backed, and a baseline SQLite database upgrades in place.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from ledger.app import create_app
from ledger.store import Ledger, LeapSwapRejected
from ledger.timecode import DEFAULT_LEAP_TABLE, LeapTable

HEADER = "telescope,exposure_id,start,duration_s\n"
LEAP = dt.date(2016, 12, 31)


@pytest.fixture()
def client(tmp_path):
    app = create_app({"TESTING": True, "LEDGER_DB": str(tmp_path / "web.sqlite3")})
    return app.test_client()


def _raw_conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _snapshot_exposures(path):
    conn = _raw_conn(path)
    try:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM exposures ORDER BY exposure_id"
            )
        ]
    finally:
        conn.close()


def _index_sql(path):
    conn = _raw_conn(path)
    try:
        return {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND tbl_name='exposures' AND sql IS NOT NULL"
            )
        }
    finally:
        conn.close()


def test_successful_swap_rewrites_no_exposure_rows(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    ledger.import_csv(
        HEADER
        + "T1,U1,2016-12-31 23:59:00,120\n"
        + "T2,U2,2015-06-30 23:59:60,1\n"
        + "T3,R1,536500804,5\n"
    )
    before = _snapshot_exposures(ledger.path)
    indexes_before = _index_sql(ledger.path)

    result = ledger.replace_leap_table("2012-06-30\n2015-06-30\n")  # drop 2016 only
    assert result["version"] == 2
    assert result["rewritten_rows"] == 0

    assert _snapshot_exposures(ledger.path) == before
    assert _index_sql(ledger.path) == indexes_before
    # Provenance columns are not retagged either.
    assert {r["exposure_id"]: r["leap_version"] for r in ledger.all_rows()} == {
        "U1": 1,
        "U2": 1,
        "R1": 1,
    }


def test_swap_emits_no_update_against_exposures(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    ledger.import_csv(HEADER + "T1,A,2016-12-31 12:00:00,60\n")

    import ledger.store as store_mod

    statements: list[str] = []
    original = store_mod._connect

    def traced(path):
        conn = original(path)
        conn.set_trace_callback(lambda sql: statements.append(" ".join(sql.split())))
        return conn

    store_mod._connect = traced
    try:
        ledger.replace_leap_table("2015-06-30\n2016-12-31\n")
    finally:
        store_mod._connect = original

    assert not [
        s
        for s in statements
        if s.startswith("UPDATE exposures") or s.startswith("DELETE FROM exposures")
    ]
    assert any(s.startswith("INSERT INTO leap_tables") for s in statements)


def test_conflict_lookup_is_served_by_index(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    ledger.import_csv(HEADER + "T1,KEEP,2016-12-31 23:59:60,1\n")
    conn = _raw_conn(ledger.path)
    try:
        ordinal = (LEAP - dt.date(2000, 1, 1)).days
        plan = " | ".join(
            row[3]
            for row in conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT exposure_id, utc_day FROM exposures "
                "INDEXED BY exposures_utc_leap60 "
                "WHERE start_source='utc' AND utc_sod=86400 AND utc_day IN (?) "
                "ORDER BY exposure_id",
                (ordinal,),
            )
        )
    finally:
        conn.close()
    assert "COVERING INDEX exposures_utc_leap60" in plan
    assert "SCAN exposures" not in plan.upper()


def test_conflicts_found_on_every_deleted_day_and_listed(tmp_path):
    ledger = Ledger(
        tmp_path / "l.sqlite3",
        initial_table=LeapTable(
            (
                dt.date(2012, 6, 30),
                dt.date(2015, 6, 30),
                dt.date(2016, 12, 31),
            )
        ),
    )
    ledger.import_csv(
        HEADER
        + "T1,A60,2015-06-30 23:59:60,1\n"
        + "T2,B60,2016-12-31 23:59:60,1\n"
        + "T3,OK,2016-12-31 23:59:59,1\n"
    )
    with pytest.raises(LeapSwapRejected) as exc:
        ledger.replace_leap_table("2012-06-30\n")  # delete 2015 and 2016
    assert sorted(o["exposure_id"] for o in exc.value.offending) == ["A60", "B60"]
    version, table = ledger.active_table()
    assert version == 1
    assert dt.date(2015, 6, 30) in table and LEAP in table


def test_utc_text_keeps_utc_moment_after_swap(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    ledger.import_csv(HEADER + "T1,X,2016-12-31 23:59:00,120\n")
    ledger.replace_leap_table("2015-06-30\n")  # remove 2016 leap
    row = ledger.all_rows()[0]
    # Frozen import-time absolute coordinate is not rewritten ...
    assert row["start_real"] == 536543944
    report = ledger.day_report(LEAP)
    item = report.rows[0]
    # ... but the preserved UTC label is reinterpreted under the new table.
    assert item["source_start_utc"] == "2016-12-31 23:59:00"
    assert item["effective_start_real"] == row["start_real"] - 3
    assert item["window_seconds"] == 60
    assert item["window_end_utc"] == "2017-01-01 00:00:00"
    assert report.day_seconds == 86400


def test_real_start_keeps_absolute_instant_after_swap(tmp_path):
    from ledger.timecode import utc_to_real

    ledger = Ledger(tmp_path / "l.sqlite3")
    noon = utc_to_real("2016-12-31 12:00:00", DEFAULT_LEAP_TABLE)
    ledger.import_csv(HEADER + f"T1,R,{noon},5\n")
    ledger.replace_leap_table("2015-06-30\n")
    row = ledger.all_rows()[0]
    assert row["start_real"] == noon
    item = ledger.day_report(LEAP).rows[0]
    assert item["effective_start_real"] == noon
    assert item["window_start_utc"] == "2016-12-31 12:00:03"


def test_day_read_correct_across_multiple_import_eras(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    ledger.import_csv(HEADER + "T1,ERA1,2016-12-31 23:59:00,120\n")
    ledger.replace_leap_table("2015-06-30\n")
    ledger.import_csv(HEADER + "T2,ERA2,2016-12-31 23:59:30,90\n")
    ledger.replace_leap_table("")  # empty table
    report = ledger.day_report(LEAP)
    ids = [r["exposure_id"] for r in report.rows]
    assert ids == ["ERA1", "ERA2"]
    by_id = {r["exposure_id"]: r for r in report.rows}
    assert by_id["ERA1"]["window_seconds"] == 60
    assert by_id["ERA2"]["window_seconds"] == 30


LEGACY_SCHEMA = """
CREATE TABLE leap_tables (
    version INTEGER PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
    source_name TEXT NOT NULL, row_count INTEGER NOT NULL,
    headers_json TEXT NOT NULL,
    leap_version INTEGER NOT NULL REFERENCES leap_tables(version));
CREATE TABLE exposures (
    exposure_id TEXT PRIMARY KEY, telescope TEXT NOT NULL,
    start_source TEXT NOT NULL CHECK (start_source IN ('utc','real')),
    start_raw TEXT NOT NULL, start_real INTEGER NOT NULL,
    duration_s INTEGER NOT NULL CHECK (duration_s BETWEEN 1 AND 3600),
    raw_json TEXT NOT NULL,
    leap_version INTEGER NOT NULL REFERENCES leap_tables(version),
    batch_id INTEGER NOT NULL REFERENCES import_batches(id));
CREATE INDEX exposures_start_real ON exposures(start_real);
CREATE INDEX exposures_start_real_id ON exposures(start_real, exposure_id);
CREATE INDEX exposures_batch ON exposures(batch_id);
"""


def _make_legacy_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO leap_tables VALUES (1, ?, '2026-01-01T00:00:00Z')",
        ("2015-06-30\n2016-12-31\n",),
    )
    conn.execute(
        "INSERT INTO import_batches VALUES (1, '2026-01-02T00:00:00Z', "
        "'old.csv', 3, ?, 1)",
        ('["telescope","exposure_id","start","duration_s"]',),
    )
    conn.execute(
        "INSERT INTO exposures VALUES "
        "('OLD60','T9','utc','2016-12-31 23:59:60',536544004,1,?,1,1)",
        ('{"start": "2016-12-31 23:59:60", "keep": "原值"}',),
    )
    conn.execute(
        "INSERT INTO exposures VALUES "
        "('OLDR','T8','real','536500804',536500804,5,?,1,1)",
        ('{"start": "536500804"}',),
    )
    conn.execute(
        "INSERT INTO exposures VALUES "
        "('OLDN','T7','utc','2016-12-31 23:59:00',536543944,120,?,1,1)",
        ('{"start": "2016-12-31 23:59:00"}',),
    )
    conn.commit()
    conn.close()


def test_legacy_database_upgrades_in_place_preserving_batches_and_raw(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    _make_legacy_db(path)

    ledger = Ledger(path)  # must upgrade without a new file
    assert ledger.stats()["exposures"] == 3
    assert ledger.stats()["batches"] == 1

    conn = _raw_conn(path)
    try:
        batch = conn.execute("SELECT source_name, row_count FROM import_batches").fetchone()
        assert tuple(batch) == ("old.csv", 3)
        raw = conn.execute(
            "SELECT raw_json FROM exposures WHERE exposure_id='OLD60'"
        ).fetchone()[0]
        assert "原值" in raw
        keys = conn.execute(
            "SELECT utc_sod, utc_key FROM exposures WHERE exposure_id='OLD60'"
        ).fetchone()
        assert keys[0] == 86400
    finally:
        conn.close()

    # Upgraded index finds the legacy conflict.
    with pytest.raises(LeapSwapRejected) as exc:
        ledger.replace_leap_table("2015-06-30\n")
    assert [o["exposure_id"] for o in exc.value.offending] == ["OLD60"]

    report = ledger.day_report(LEAP)
    assert {r["exposure_id"]: r["window_seconds"] for r in report.rows} == {
        "OLDN": 61,
        "OLD60": 1,
        "OLDR": 5,
    }

    # Reopening is a no-op and keeps data.
    Ledger(path)
    assert ledger.stats()["exposures"] == 3


def test_csv_after_swap_uses_new_table_window(client):
    client.post(
        "/import",
        data={"csv_text": HEADER + "T1,A,2016-12-31 23:59:00,120\n"},
    )
    client.post("/leap", data={"leap_text": "2015-06-30\n"})
    body = client.get("/day.csv?date=2016-12-31").get_data(as_text=True)
    assert ",A,2016-12-31 23:59:00,2017-01-01 00:00:00,60," in body
