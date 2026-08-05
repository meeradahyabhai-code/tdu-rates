"""Publish the currently-effective Texas TDU rates as JSON, for consumers that
want today's number without parsing the whole history CSV.

Deliberately carries no timestamp: the file changes only when a rate changes, so a
diff against it is a reliable "something moved" signal. Provenance is the CSV hash
plus the commit that carried it.

Run: python3 scripts/emit_json.py [--today MM/DD/YYYY] [--out data/tdu-rates.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import puc_tdu as m  # noqa: E402

CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "tdsp_charges.csv")

NAMES = {
    "ONCOR": "Oncor Electric Delivery",
    "CNP": "CenterPoint Energy Houston Electric",
    "AEPCC": "AEP Texas Central",
    "AEPNC": "AEP Texas North",
    "TNMP": "Texas-New Mexico Power",
}


def current_row(rows: list[dict], utility: str, today: date) -> dict | None:
    """The row in force today, else the newest row if today falls past the last one."""
    hits = [r for r in rows if r["utility"] == utility and r["startDate"] and r["endDate"]]
    if not hits:
        return None
    live = [r for r in hits if m._d(r["startDate"]) <= today <= m._d(r["endDate"])]
    return live[-1] if live else max(hits, key=lambda r: m._d(r["startDate"]))


def build(csv_path: str, today: date) -> dict:
    rows = m.read_csv(csv_path)
    digest = hashlib.sha256(open(csv_path, "rb").read()).hexdigest()

    utilities, stale = {}, []
    for code in m.UTILITIES:
        row = current_row(rows, code, today)
        if row is None:
            raise SystemExit(f"{code}: no dated rows in {csv_path}")
        expired = m._d(row["endDate"]) < today
        if expired:
            stale.append(code)
        utilities[code] = {
            "name": NAMES[code],
            "monthlyCharge": float(row["monthly"]),
            "perKwh": float(row["perKwh"]),
            "startDate": row["startDate"],
            "endDate": row["endDate"],
            "expired": expired,
        }

    return {
        "source": m.PAGE_URL,
        "reports": m.FTP_BASE,
        "coverage": "Texas TDU residential delivery charges only",
        "csvSha256": digest,
        "utilities": utilities,
        "expiredUtilities": stale,
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", default=CSV)
    p.add_argument("--out", default=os.path.join(os.path.dirname(CSV), "tdu-rates.json"))
    p.add_argument("--today", help="MM/DD/YYYY, for testing")
    p.add_argument("--check", action="store_true",
                   help="exit 1 if the file on disk is out of date, write nothing")
    a = p.parse_args(argv)

    today = datetime.strptime(a.today, "%m/%d/%Y").date() if a.today else date.today()
    payload = json.dumps(build(a.csv, today), indent=2) + "\n"

    if a.check:
        existing = open(a.out).read() if os.path.exists(a.out) else ""
        if existing != payload:
            print(f"{a.out} is out of date", file=sys.stderr)
            return 1
        print(f"{a.out} is current")
        return 0

    with open(a.out, "w") as f:
        f.write(payload)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
