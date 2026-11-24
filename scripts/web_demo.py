#!/usr/bin/env python3
"""End-to-end web demo for leap-table before/after day detail.

Drives a running server over HTTP only (stdlib, no requests needed):

  1. imports a 2016-12-31 cross-midnight night plus a 2015 ``23:59:60`` row;
  2. fetches the day page and CSV *before* the swap (86401 s day, 61/2 s clips);
  3. tries a table that deletes both leap days -> rejected, ids listed, old table;
  4. swaps to a table that deletes only 2016-12-31 -> success, zero rewrites;
  5. fetches the same day *after* (86400 s day, 60/1 s clips).

Exposure ids get a run suffix so re-importing into a database that already saw
the conflict rows does not collide.  The before/after transition itself assumes
a database still on its initial leap table (e.g. a fresh volume).

Usage:  python scripts/web_demo.py [base_url]
"""

from __future__ import annotations

import csv
import io
import sys
import time
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
RUN = str(int(time.time()))
DAY = "2016-12-31"

failures: list[str] = []


def check(name: str, condition: bool, evidence: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f"  {evidence}" if evidence else ""))
    if not condition:
        failures.append(name)


def post_form(path: str, fields: dict[str, str]) -> str:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", "replace")


def get_text(path: str) -> tuple[int, str]:
    with urllib.request.urlopen(BASE + path, timeout=15) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def day_csv_seconds() -> dict[str, int]:
    _status, body = get_text(f"/day.csv?date={DAY}")
    rows = csv.DictReader(io.StringIO(body))
    return {row["exposure_id"]: int(row["seconds"]) for row in rows}


def main() -> int:
    cross = f"DEMO{RUN}-CROSS"
    last = f"DEMO{RUN}-LAST"
    c60 = f"DEMO{RUN}-C60"
    csv_text = (
        "telescope,exposure_id,start,duration_s\n"
        f"T1,{cross},2016-12-31 23:59:00,120\n"
        f"T2,{last},2016-12-31 23:59:59,2\n"
        f"T3,DEMO{RUN}-NOONU,2016-12-31 12:00:00,60\n"
        f"T4,DEMO{RUN}-NOONR,536500804,5\n"
        f"T5,{c60},2015-06-30 23:59:60,1\n"
    )

    page = post_form("/import", {"csv_text": csv_text})
    check("import batch accepted", "导入完成" in page, f"run={RUN}")

    status, before_page = get_text(f"/day?date={DAY}")
    before_csv = day_csv_seconds()
    check("before: day page 200", status == 200)
    check("before: leap day is 86401 s", "是（86401 秒）" in before_page)
    check("before: last second 23:59:60 shown", "2016-12-31 23:59:60" in before_page)
    check("before: CROSS clips to 61 s", before_csv.get(cross) == 61, str(before_csv))
    check("before: LAST clips to 2 s", before_csv.get(last) == 2, str(before_csv))

    # Candidate deleting 2015-06-30 too -> index finds the :60 conflict.
    rejected = post_form(
        "/leap",
        {
            "leap_text": "2012-06-30\n2016-12-31\n",
            "review_date": DAY,
        },
    )
    check("conflict swap rejected banner", "换表未生效" in rejected)
    check("conflict exposure id listed", c60 in rejected)

    _status, still_before = get_text(f"/day?date={DAY}")
    check("old table still active (86401)", "是（86401 秒）" in still_before)

    # Delete only 2016-12-31; the 2015 :60 row stays valid.
    ok = post_form(
        "/leap",
        {
            "leap_text": "2012-06-30\n2015-06-30\n",
            "review_date": DAY,
        },
    )
    check("clean swap enabled v2", "已启用闰秒表 v2" in ok)
    check("clean swap reports zero rewrites", "重写行数 0" in ok)

    status, after_page = get_text(f"/day?date={DAY}")
    after_csv = day_csv_seconds()
    check("after: day page 200", status == 200)
    check("after: day is 86400 s", "否（86400 秒）" in after_page)
    check("after: leap marker gone", "是（86401 秒）" not in after_page)
    check("after: CROSS clips to 60 s", after_csv.get(cross) == 60, str(after_csv))
    check("after: LAST clips to 1 s", after_csv.get(last) == 1, str(after_csv))
    check(
        "after: source UTC label preserved",
        "2016-12-31 23:59:00" in after_page,
    )

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {failures}")
        return 1
    print("ALL WEB DEMO CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
