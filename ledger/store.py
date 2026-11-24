"""SQLite storage and batch validation for the exposure ledger.

Data model
----------
Each imported CSV row keeps its original cell values (``raw_json``, keyed by
the exact header names from the uploaded file) and its raw start token
(``start_raw``).  A start token is either UTC text - which may use
``23:59:60`` - or an integer count of real seconds since
2000-01-01 00:00:00 UTC.

Two table-independent coordinates make leap-table replacements cheap:

* ``start_real`` is the absolute instant for rows submitted as real seconds
  and stays untouched for every row after import.  Real-second day windows use
  the existing ``exposures_start_real_id`` index.
* ``utc_day`` / ``utc_sod`` / ``utc_key`` encode the literal UTC label (every
  civil day gets an 86401-slot block, so ``23:59:60`` is slot 86400).  They do
  NOT depend on the leap table and are never rewritten by a swap.  UTC-text
  day windows use the partial ``exposures_utc_key`` index; the partial
  covering index ``exposures_utc_leap60`` answers swap validation.

At read time a UTC-text row is reinterpreted from its preserved ``start_raw``
under the single leap-table version loaded for the fetch ("preserve the UTC
moment"), while a real-second row keeps using its frozen ``start_real``
("preserve the absolute instant").

Leap tables are immutable, versioned rows.  Replacement validates the whole
candidate table in one transaction.  The only stored starts that can become
invalid are ``23:59:60`` UTC starts on a leap day the candidate deletes; those
are found solely through the leap-60 index.  On conflict the swap is rejected,
the offending exposure ids are returned and the previous table stays active.
A successful swap inserts one new ``leap_tables`` row and never updates,
deletes or reindexes ``exposures``.

Writers serialise with an in-process lock and ``BEGIN IMMEDIATE``.  Readers
use a WAL snapshot and load exactly one leap-table version per request, so a
day fetch always uses one consistent committed state even while a replacement
or import is running.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from .timecode import (
    DEFAULT_LEAP_TABLE,
    EPOCH,
    EPOCH_DATE,
    LeapTable,
    TimecodeError,
    day_range_real,
    day_utc_key_window,
    fmt_utc,
    format_leap_table,
    normalize_utc_text,
    parse_leap_table,
    parse_utc,
    real_to_utc,
    utc_parts_from_text,
    utc_to_real,
)

CSV_FIELDS = ("telescope", "exposure_id", "start", "duration_s")
MIN_DURATION = 1
MAX_DURATION = 3600
MAX_SWAP_REPORT = 500
EARLIEST_DAY = EPOCH.date()
LATEST_DAY = _dt.date(9999, 12, 30)
INTEGER_RE = re.compile(r"^[+-]?\d+$")


def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LedgerError(Exception):
    """Base class for user-facing ledger errors."""


class ValidationError(LedgerError):
    """A submitted batch is invalid; nothing was written."""

    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.details = list(details or [])


class LeapSwapRejected(LedgerError):
    """The candidate leap table would invalidate stored UTC text."""

    def __init__(self, offending: list[dict]):
        super().__init__(
            "the new leap table would invalidate stored UTC times; "
            "the previous table remains active"
        )
        self.offending = offending


@dataclass(frozen=True)
class ParsedBatch:
    headers: tuple[str, ...]
    rows: list[dict]


@dataclass(frozen=True)
class DayReport:
    day: _dt.date
    leap_version: int
    leap_day: bool
    day_start_utc: str
    day_end_utc: str
    day_last_utc: str
    day_start_real: int
    day_end_real: int
    day_seconds: int
    rows: list[dict]
    total_rows: int
    total_seconds: int


def _connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    # WAL lets day reads proceed on a snapshot while an import or leap-table
    # replacement writes.  The mode persists in the database file.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


# Fresh databases get the stable UTC-key columns directly.  Legacy databases
# (created by the previous all-rewrite implementation) have the same tables
# without those columns; ``_migrate_schema`` adds them in place.
SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS leap_tables (
    version    INTEGER PRIMARY KEY,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    source_name  TEXT NOT NULL,
    row_count    INTEGER NOT NULL,
    headers_json TEXT NOT NULL,
    leap_version INTEGER NOT NULL REFERENCES leap_tables(version)
);

CREATE TABLE IF NOT EXISTS exposures (
    exposure_id  TEXT PRIMARY KEY,
    telescope    TEXT NOT NULL,
    start_source TEXT NOT NULL CHECK (start_source IN ('utc', 'real')),
    start_raw    TEXT NOT NULL,
    start_real   INTEGER NOT NULL,
    duration_s   INTEGER NOT NULL CHECK (duration_s BETWEEN 1 AND 3600),
    raw_json     TEXT NOT NULL,
    leap_version INTEGER NOT NULL REFERENCES leap_tables(version),
    batch_id     INTEGER NOT NULL REFERENCES import_batches(id),
    utc_day      INTEGER,
    utc_sod      INTEGER,
    utc_key      INTEGER
);
"""

# Baseline indexes are preserved exactly.  The two partial UTC indexes are
# what make text-row day windows and leap-60 conflict checks index-only /
# index-range operations.
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS exposures_start_real ON exposures(start_real);
CREATE INDEX IF NOT EXISTS exposures_start_real_id ON exposures(start_real, exposure_id);
CREATE INDEX IF NOT EXISTS exposures_batch ON exposures(batch_id);
CREATE INDEX IF NOT EXISTS exposures_utc_key
    ON exposures(utc_key, exposure_id)
    WHERE start_source = 'utc';
CREATE INDEX IF NOT EXISTS exposures_utc_leap60
    ON exposures(utc_sod, utc_day, exposure_id, start_source);
"""

#: Full idempotent schema for a fresh database.
SCHEMA = SCHEMA_TABLES + SCHEMA_INDEXES

_EXPOSURE_COLUMNS = (
    "exposure_id, telescope, start_source, start_raw, start_real, duration_s"
)


# ---------------------------------------------------------------------------
# CSV parsing and row validation
# ---------------------------------------------------------------------------


def _blank(text: str | None) -> bool:
    return text is None or not text.strip()


def _too_many_digits(text: str) -> bool:
    """Guard ``int()`` against pathological digit strings.

    Python 3.11 refuses to parse very long integer strings; the supported
    instant range needs at most 12 digits, so anything past 19 is out of range
    regardless and can be rejected without converting.
    """

    return len(text.lstrip("+-")) > 19


def _sniff_header(fieldnames: list[str]) -> dict[str, str]:
    """Map exact original header names to canonical field names.

    The whole header is validated at once: unknown, duplicate or missing
    columns reject the batch before any row is looked at.
    """

    cleaned = [(name or "").strip() for name in fieldnames]
    if len(set(cleaned)) != len(cleaned):
        raise ValidationError(
            "CSV rejected", ["the header contains duplicate column names"]
        )
    if any(not name for name in cleaned):
        raise ValidationError(
            "CSV rejected", ["the header contains an empty column name"]
        )

    if set(cleaned) != set(CSV_FIELDS):
        unknown = [name for name in cleaned if name not in CSV_FIELDS]
        missing = [name for name in CSV_FIELDS if name not in cleaned]
        details = []
        if missing:
            details.append(f"missing column(s): {', '.join(missing)}")
        if unknown:
            details.append(f"unexpected column(s): {', '.join(unknown)}")
        details.append(f"expected header: {','.join(CSV_FIELDS)}")
        raise ValidationError("CSV rejected", details)
    return {name: name for name in cleaned}


def parse_csv(text: str) -> ParsedBatch:
    """Parse a CSV batch into raw rows without validating start times."""

    if _blank(text):
        raise ValidationError("CSV rejected", ["no CSV text was supplied"])
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise ValidationError("CSV rejected", ["the CSV is empty"])
    mapping = _sniff_header(reader.fieldnames)
    headers = tuple((name or "").strip() for name in reader.fieldnames)

    rows: list[dict] = []
    for record in reader:
        if None in record or len(record) != len(reader.fieldnames):
            raise ValidationError(
                "CSV rejected",
                [f"line {reader.line_num}: wrong number of columns"],
            )
        original = {
            (key or "").strip(): "" if value is None else value
            for key, value in record.items()
        }
        canonical = {mapping[key]: value for key, value in original.items()}
        if not any(value.strip() for value in canonical.values()):
            continue  # blank line
        rows.append(
            {
                "line": reader.line_num,
                "raw": canonical,
                "raw_original": original,
            }
        )
    if not rows:
        raise ValidationError("CSV rejected", ["the CSV has a header but no data rows"])
    return ParsedBatch(headers=headers, rows=rows)


def _validate_row(row: dict, table: LeapTable) -> list[str]:
    """Validate one raw row against the leap table selected at submit time."""

    line = row["line"]
    raw = row["raw"]
    errors: list[str] = []
    telescope = raw["telescope"].strip()
    exposure_id = raw["exposure_id"].strip()
    start_text = raw["start"].strip()
    duration_text = raw["duration_s"].strip()

    if not telescope:
        errors.append(f"line {line}: telescope is required")
    elif len(telescope) > 64:
        errors.append(f"line {line}: telescope is longer than 64 characters")
    if not exposure_id:
        errors.append(f"line {line}: exposure_id is required")
    elif len(exposure_id) > 128:
        errors.append(f"line {line}: exposure_id is longer than 128 characters")

    start_real: int | None = None
    start_source: str | None = None
    utc_day: int | None = None
    utc_sod: int | None = None
    utc_key: int | None = None
    if not start_text:
        errors.append(f"line {line}: start is required")
    elif INTEGER_RE.match(start_text):
        if _too_many_digits(start_text):
            errors.append(f"line {line}: start real seconds are too large")
        else:
            value = int(start_text)
            if value < 0:
                errors.append(
                    f"line {line}: real seconds must be at or after "
                    "2000-01-01 00:00:00 UTC (>= 0)"
                )
            else:
                try:
                    start_day = real_to_utc(value, table)[0].date()
                except (TimecodeError, OverflowError):
                    start_day = _dt.date.max
                if start_day > LATEST_DAY:
                    errors.append(
                        f"line {line}: start is outside the supported range "
                        f"(latest day is {LATEST_DAY.isoformat()})"
                    )
                else:
                    start_real = value
                    start_source = "real"
    else:
        try:
            parse_utc(start_text, table)
            start_real = utc_to_real(start_text, table)
        except TimecodeError as exc:
            errors.append(f"line {line}: {exc}")
        else:
            if start_real < 0:
                errors.append(f"line {line}: start is before 2000-01-01 00:00:00 UTC")
            elif parse_utc(start_text, table).date() > LATEST_DAY:
                errors.append(
                    f"line {line}: start is outside the supported range "
                    f"(latest day is {LATEST_DAY.isoformat()})"
                )
            else:
                start_source = "utc"
                # Table-independent label key; kept stable across swaps.
                utc_day, utc_sod, utc_key = utc_parts_from_text(start_text)

    duration: int | None = None
    if not duration_text:
        errors.append(f"line {line}: duration_s is required")
    elif not duration_text.isascii() or not duration_text.isdigit():
        errors.append(
            f"line {line}: duration_s must be a whole number of seconds, "
            f"got {duration_text!r}"
        )
    elif _too_many_digits(duration_text):
        errors.append(
            f"line {line}: duration_s must be between {MIN_DURATION} and "
            f"{MAX_DURATION}, got {duration_text[:20]}…"
        )
    else:
        value = int(duration_text)
        if not (MIN_DURATION <= value <= MAX_DURATION):
            errors.append(
                f"line {line}: duration_s must be between {MIN_DURATION} and "
                f"{MAX_DURATION}, got {value}"
            )
        else:
            duration = value

    row.update(
        {
            "telescope": telescope,
            "exposure_id": exposure_id,
            "start_source": start_source,
            "start_raw": start_text,
            "start_real": start_real,
            "utc_day": utc_day,
            "utc_sod": utc_sod,
            "utc_key": utc_key,
            "duration_s": duration,
            "raw_json": json.dumps(
                row["raw_original"], ensure_ascii=False, sort_keys=True
            ),
        }
    )
    return errors


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class Ledger:
    """One SQLite database, safe for Flask's threaded request handling."""

    def __init__(self, path: str | Path, initial_table: LeapTable | None = None):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._write_lock:
            conn = _connect(self.path)
            try:
                conn.executescript(SCHEMA_TABLES)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    count = conn.execute("SELECT COUNT(*) FROM leap_tables").fetchone()[
                        0
                    ]
                    if count == 0:
                        table = initial_table or DEFAULT_LEAP_TABLE
                        conn.execute(
                            "INSERT OR IGNORE INTO leap_tables(version, body, created_at) "
                            "VALUES(1, ?, ?)",
                            (format_leap_table(table), _iso_now()),
                        )
                    conn.execute("COMMIT")
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise

                # In-place upgrade of legacy databases, then idempotent indexes.
                self._migrate_columns(conn)
                _version, table = self._active_table_tx(conn)
                self._backfill_utc_keys(conn, table)
                conn.executescript(SCHEMA_INDEXES)
            finally:
                conn.close()

    # -- schema migration --------------------------------------------------

    @staticmethod
    def _migrate_columns(conn: sqlite3.Connection) -> list[str]:
        """Add stable UTC-key columns to an old database (in place)."""

        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(exposures)")
        }
        added: list[str] = []
        for name, declaration in (
            ("utc_day", "INTEGER"),
            ("utc_sod", "INTEGER"),
            ("utc_key", "INTEGER"),
        ):
            if name not in existing:
                conn.execute(
                    f"ALTER TABLE exposures ADD COLUMN {name} {declaration}"
                )
                added.append(name)
        return added

    @staticmethod
    def _backfill_utc_keys(conn: sqlite3.Connection, table: LeapTable) -> int:
        """Fill stable UTC keys for pre-existing text rows once.

        Raw text is enough on its own.  As a defensive fallback for a token
        that no longer normalises, derive the label from the frozen
        ``start_real`` under the active table.
        """

        rows = conn.execute(
            "SELECT exposure_id, start_raw, start_real FROM exposures "
            "WHERE start_source = 'utc' AND utc_key IS NULL"
        ).fetchall()
        if not rows:
            return 0
        updates: list[tuple[int, int, int, str]] = []
        for row in rows:
            try:
                utc_day, utc_sod, utc_key = utc_parts_from_text(row["start_raw"])
            except TimecodeError:
                real = int(row["start_real"])
                dt_value, on_leap = real_to_utc(real, table)
                day = dt_value.date()
                second_of_day = (
                    dt_value.hour * 3600
                    + dt_value.minute * 60
                    + dt_value.second
                    + (1 if on_leap else 0)
                )
                ordinal = (day - EPOCH_DATE).days
                utc_day, utc_sod = ordinal, second_of_day
                utc_key = ordinal * 86401 + second_of_day
            updates.append((utc_day, utc_sod, utc_key, row["exposure_id"]))
        conn.execute("BEGIN")
        try:
            conn.executemany(
                "UPDATE exposures SET utc_day = ?, utc_sod = ?, utc_key = ? "
                "WHERE exposure_id = ?",
                updates,
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return len(updates)

    # -- read side ---------------------------------------------------------

    def active_table(self) -> tuple[int, LeapTable]:
        conn = _connect(self.path)
        try:
            return self._active_table_tx(conn)
        finally:
            conn.close()

    @staticmethod
    def _active_table_tx(conn: sqlite3.Connection) -> tuple[int, LeapTable]:
        row = conn.execute(
            "SELECT version, body FROM leap_tables ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return int(row["version"]), parse_leap_table(row["body"])

    def leap_history(self) -> list[dict]:
        conn = _connect(self.path)
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT version, body, created_at FROM leap_tables ORDER BY version"
                )
            ]
        finally:
            conn.close()

    def stats(self) -> dict:
        conn = _connect(self.path)
        try:
            exposures = conn.execute("SELECT COUNT(*) FROM exposures").fetchone()[0]
            batches = conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0]
            version = conn.execute("SELECT MAX(version) FROM leap_tables").fetchone()[0]
            return {
                "exposures": int(exposures),
                "batches": int(batches),
                "leap_version": int(version),
            }
        finally:
            conn.close()

    def day_report(self, day: _dt.date) -> DayReport:
        """Fetch exposures intersecting one UTC civil day.

        One read transaction supplies both the leap table and the rows, so the
        day boundaries, index windows, clipping and formatting all use the
        same committed table/exposure snapshot even if a replacement or import
        commits concurrently.

        Two index range reads supply candidates:

        * UTC-text rows via the table-independent ``utc_key`` label window;
        * real-second rows via ``start_real`` in
          ``[day_start - 3600, day_end)``.

        Exact intersection and half-open clipping are then computed under the
        active table from each row's preserved coordinate.
        """

        if not isinstance(day, _dt.date):
            raise ValidationError("date rejected", ["choose a UTC date"])
        if day < EARLIEST_DAY:
            raise ValidationError(
                "date rejected", ["the ledger starts at 2000-01-01 UTC"]
            )
        if day > LATEST_DAY:
            raise ValidationError(
                "date rejected",
                [f"the latest supported day is {LATEST_DAY.isoformat()}"],
            )

        conn = _connect(self.path)
        try:
            conn.execute("BEGIN")
            version, table = self._active_table_tx(conn)
            start_real, end_real = day_range_real(day, table)
            key_low, key_high = day_utc_key_window(day)

            utc_candidates = conn.execute(
                f"SELECT {_EXPOSURE_COLUMNS} FROM exposures "
                "INDEXED BY exposures_utc_key "
                "WHERE start_source = 'utc' AND utc_key >= ? AND utc_key < ?",
                (key_low, key_high),
            ).fetchall()
            real_candidates = conn.execute(
                f"SELECT {_EXPOSURE_COLUMNS} FROM exposures "
                "INDEXED BY exposures_start_real_id "
                "WHERE start_source = 'real' "
                "AND start_real >= ? AND start_real < ?",
                (start_real - MAX_DURATION, end_real),
            ).fetchall()

            rows: list[dict] = []
            for row in (*utc_candidates, *real_candidates):
                clipped = _clip_row(row, start_real, end_real, table)
                if clipped is not None:
                    rows.append(clipped)
            rows.sort(
                key=lambda item: (
                    item["effective_start_real"],
                    item["exposure_id"],
                )
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        start_dt, start_leap = real_to_utc(start_real, table)
        last_dt, last_leap = real_to_utc(end_real - 1, table)
        return DayReport(
            day=day,
            leap_version=version,
            leap_day=day in table,
            day_start_utc=fmt_utc(start_dt, start_leap),
            day_end_utc=fmt_utc(*real_to_utc(end_real, table)),
            day_last_utc=fmt_utc(last_dt, last_leap),
            day_start_real=start_real,
            day_end_real=end_real,
            day_seconds=end_real - start_real,
            rows=rows,
            total_rows=len(rows),
            total_seconds=sum(row["window_seconds"] for row in rows),
        )

    def all_rows(self) -> list[dict]:
        conn = _connect(self.path)
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT exposure_id, telescope, start_source, start_raw, start_real, "
                    "duration_s, leap_version, batch_id FROM exposures ORDER BY exposure_id"
                )
            ]
        finally:
            conn.close()

    # -- write side --------------------------------------------------------

    def import_csv(self, text: str, source_name: str = "upload.csv") -> dict:
        """Validate and insert one CSV batch under the active leap table.

        The batch is all-or-nothing.  Duplicate ids inside the batch and ids
        already stored reject the whole batch.  Validation happens inside
        ``BEGIN IMMEDIATE`` against the table committed at submit time.
        """

        batch = parse_csv(text)
        with self._write_lock:
            conn = _connect(self.path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                version, table = self._active_table_tx(conn)
                errors: list[str] = []
                for row in batch.rows:
                    errors.extend(_validate_row(row, table))
                seen: dict[str, int] = {}
                for row in batch.rows:
                    prior = seen.setdefault(row["exposure_id"], row["line"])
                    if prior != row["line"]:
                        errors.append(
                            f"line {row['line']}: exposure id {row['exposure_id']!r} "
                            f"duplicates line {prior}"
                        )
                if errors:
                    raise ValidationError(
                        "import rejected; nothing was written", errors
                    )

                cursor = conn.execute(
                    "INSERT INTO import_batches(created_at, source_name, row_count, "
                    "headers_json, leap_version) VALUES(?, ?, ?, ?, ?)",
                    (
                        _iso_now(),
                        source_name or "upload.csv",
                        len(batch.rows),
                        json.dumps(list(batch.headers), ensure_ascii=False),
                        version,
                    ),
                )
                batch_id = int(cursor.lastrowid)
                try:
                    conn.executemany(
                        "INSERT INTO exposures(exposure_id, telescope, start_source, start_raw, "
                        "start_real, duration_s, raw_json, leap_version, batch_id, "
                        "utc_day, utc_sod, utc_key) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                row["exposure_id"],
                                row["telescope"],
                                row["start_source"],
                                row["start_raw"],
                                row["start_real"],
                                row["duration_s"],
                                row["raw_json"],
                                version,
                                batch_id,
                                row["utc_day"],
                                row["utc_sod"],
                                row["utc_key"],
                            )
                            for row in batch.rows
                        ],
                    )
                except sqlite3.IntegrityError as exc:
                    if "exposures.exposure_id" in str(exc):
                        raise ValidationError(
                            "import rejected; nothing was written",
                            [
                                "an exposure id in this batch already exists in the ledger; "
                                "the whole batch was rolled back"
                            ],
                        ) from exc
                    raise ValidationError(
                        "import rejected; nothing was written", [str(exc)]
                    ) from exc
                conn.execute("COMMIT")
                return {
                    "batch_id": batch_id,
                    "row_count": len(batch.rows),
                    "leap_version": version,
                    "leap_days": len(table.dates),
                }
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    # -- leap-table replacement -------------------------------------------

    @staticmethod
    def _deleted_days(old_table: LeapTable, new_table: LeapTable) -> list[_dt.date]:
        return sorted(set(old_table.dates) - set(new_table.dates))

    def _leap60_conflicts_tx(
        self,
        conn: sqlite3.Connection,
        deleted_days: list[_dt.date],
    ) -> list[dict]:
        """Index-only lookup of stored ``23:59:60`` starts on deleted days.

        The query is served by the partial covering index
        ``exposures_utc_leap60`` (``utc_day, exposure_id`` for UTC rows whose
        literal second-of-day is 86400); no full exposure scan or Python
        re-validation of every stored timestamp is performed.  Display fields
        are then fetched by primary key.
        """

        if not deleted_days:
            return []
        ordinals = [(day - EPOCH_DATE).days for day in deleted_days]
        day_by_ordinal = {
            (day - EPOCH_DATE).days: day.isoformat() for day in deleted_days
        }
        placeholders = ",".join("?" for _ in ordinals)
        index_rows = conn.execute(
            "SELECT exposure_id, utc_day FROM exposures "
            "INDEXED BY exposures_utc_leap60 "
            "WHERE start_source = 'utc' AND utc_sod = 86400 "
            f"AND utc_day IN ({placeholders}) "
            "ORDER BY exposure_id",
            ordinals,
        ).fetchall()
        if not index_rows:
            return []

        ordinal_by_id = {
            row["exposure_id"]: int(row["utc_day"]) for row in index_rows
        }
        ids = list(ordinal_by_id)
        id_placeholders = ",".join("?" for _ in ids)
        details = {
            row["exposure_id"]: row
            for row in conn.execute(
                "SELECT exposure_id, telescope, start_raw FROM exposures "
                f"WHERE exposure_id IN ({id_placeholders})",
                ids,
            )
        }
        offending: list[dict] = []
        for exposure_id in ids:
            detail = details.get(exposure_id)
            day_text = day_by_ordinal.get(
                ordinal_by_id[exposure_id], str(ordinal_by_id[exposure_id])
            )
            offending.append(
                {
                    "exposure_id": exposure_id,
                    "telescope": detail["telescope"] if detail else "",
                    "start": detail["start_raw"] if detail else f"{day_text} 23:59:60",
                    "error": (
                        f"{day_text} is removed from the new leap table, so "
                        "23:59:60 no longer exists on that UTC day"
                    ),
                }
            )
        return offending

    def preview_leap_table(self, text: str) -> dict:
        """Parse a candidate table and report index-found conflicts.

        Read-only; uses the same committed table and index lookup as
        :meth:`replace_leap_table`.
        """

        new_table = parse_leap_table(text)
        conn = _connect(self.path)
        try:
            conn.execute("BEGIN")
            old_version, old_table = self._active_table_tx(conn)
            deleted = self._deleted_days(old_table, new_table)
            offending = self._leap60_conflicts_tx(conn, deleted)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return {
            "version": old_version,
            "table": new_table,
            "deleted_days": deleted,
            "offending": offending,
        }

    def replace_leap_table(self, text: str) -> dict:
        """Validate and activate a complete positive-leap date table.

        Validation only checks stored ``23:59:60`` UTC starts on days the
        candidate deletes, through the leap-60 index.  A conflict aborts the
        swap, reports the offending exposure ids and leaves the previous table
        active.  Success inserts a new immutable ``leap_tables`` version only:
        exposure rows and their time indexes are preserved exactly.
        """

        try:
            new_table = parse_leap_table(text)
        except TimecodeError as exc:
            raise ValidationError(
                "leap table rejected; nothing was changed", [str(exc)]
            ) from exc

        with self._write_lock:
            conn = _connect(self.path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                old_version, old_table = self._active_table_tx(conn)
                deleted_days = self._deleted_days(old_table, new_table)
                offending = self._leap60_conflicts_tx(conn, deleted_days)
                if offending:
                    raise LeapSwapRejected(offending)

                if new_table == old_table:
                    conn.execute("COMMIT")
                    return {
                        "version": old_version,
                        "previous_version": old_version,
                        "updated_text_rows": 0,
                        "rewritten_rows": 0,
                        "rows_rewritten": 0,
                        "rewritten_exposures": 0,
                        "unchanged": True,
                        "conflict_count": 0,
                        "deleted_days": [d.isoformat() for d in deleted_days],
                        "leap_days": len(new_table.dates),
                    }

                new_version = old_version + 1
                conn.execute(
                    "INSERT INTO leap_tables(version, body, created_at) VALUES(?, ?, ?)",
                    (new_version, format_leap_table(new_table), _iso_now()),
                )
                # Intentionally NO updates to exposures or its indexes.
                conn.execute("COMMIT")
                return {
                    "version": new_version,
                    "previous_version": old_version,
                    # Kept as a key for older templates/callers; always zero now.
                    "updated_text_rows": 0,
                    "rewritten_rows": 0,
                    "rows_rewritten": 0,
                    "rewritten_exposures": 0,
                    "unchanged": False,
                    "conflict_count": 0,
                    "deleted_days": [d.isoformat() for d in deleted_days],
                    "leap_days": len(new_table.dates),
                }
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()


def _clip_row(
    row: sqlite3.Row,
    day_start: int,
    day_end: int,
    table: LeapTable,
) -> dict | None:
    """Clip one candidate row to ``[day_start, day_end)``.

    Returns ``None`` when the strict half-open intersection is empty.  The
    effective absolute start is taken from the preserved UTC label under the
    active table for UTC-text rows, and from frozen ``start_real`` for
    real-second rows.
    """

    duration = int(row["duration_s"])
    if row["start_source"] == "utc":
        try:
            real_start = utc_to_real(row["start_raw"], table)
        except TimecodeError:
            # The swap transaction guarantees this cannot happen to committed
            # data; keep the fetch available if a legacy file breaks the rule.
            real_start = int(row["start_real"])
    else:
        real_start = int(row["start_real"])

    real_end = real_start + duration
    # Half-open intersection: [start, end) and [day_start, day_end).
    if real_start >= day_end or real_end <= day_start:
        return None

    clip_start = max(real_start, day_start)
    clip_end = min(real_end, day_end)

    if row["start_source"] == "utc":
        # Preserved UTC moment: show the canonicalised original label rather
        # than rendering the frozen import-time coordinate through a new table.
        source_start_utc = normalize_utc_text(row["start_raw"])
    else:
        source_dt, source_leap = real_to_utc(real_start, table)
        source_start_utc = fmt_utc(source_dt, source_leap)
    start_dt, start_leap = real_to_utc(clip_start, table)
    end_dt, end_leap = real_to_utc(clip_end, table)
    return {
        "exposure_id": row["exposure_id"],
        "telescope": row["telescope"],
        "start_source": row["start_source"],
        "source_start": row["start_raw"],
        "source_start_utc": source_start_utc,
        "source_duration_s": duration,
        "effective_start_real": real_start,
        "window_start_utc": fmt_utc(start_dt, start_leap),
        "window_end_utc": fmt_utc(end_dt, end_leap),
        "window_start_real": clip_start,
        "window_end_real": clip_end,
        "window_seconds": clip_end - clip_start,
        "whole_day": clip_start == day_start and clip_end == day_end,
        "starts_before_day": real_start < day_start,
        "ends_after_day": real_end > day_end,
    }
