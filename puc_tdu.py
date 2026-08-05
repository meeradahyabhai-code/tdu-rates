"""Pull Texas TDU residential delivery rates from the PUCT and keep tdsp_charges.csv current.

Two independent sources, both from puc.texas.gov, and they must agree:
  1. the monthly rate-report PDF each TDU files (authoritative, carries the effective date)
  2. the HTML table on /industry/electric/rates/tdr/ (cross-check)

If they disagree, or a published average-bill figure doesn't reconcile, this exits
non-zero and writes nothing. A wrong delivery rate is worse than a stale one.
"""

from __future__ import annotations

import calendar
import csv
import io
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

PAGE_URL = "https://www.puc.texas.gov/industry/electric/rates/tdr/"
FTP_BASE = "https://ftp.puc.texas.gov/public/puct-info/industry/electric/rates/tdr/tdu/"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) tdsp-rates/1.0"}

# CSV utility code -> (PDF file stem, label on the PUCT html table)
UTILITIES = {
    "ONCOR": ("Oncor", "Oncor"),
    "CNP": ("CenterPoint", "CenterPoint"),
    "AEPCC": ("AEP", "AEP Central"),
    "AEPNC": ("AEP", "AEP North"),
    "TNMP": ("TNMP", "Texas-New Mexico Power"),
}



@dataclass
class Rate:
    utility: str
    monthly: float  # customer charge + metering charge
    per_kwh: float  # volumetric charge as published
    effective: date
    bill_1000: float  # PUCT's own average 1,000 kWh bill, used as a checksum
    source: str

    @property
    def derived_per_kwh(self) -> float:
        """per-kWh implied by the published average bill. Catches rounded volumetrics."""
        return round((self.bill_1000 - self.monthly) / 1000, 6)


def _f(s: str) -> float:
    return float(s.replace(",", "").replace("$", "").strip())


_BUNDLE: str | None = None


def _ca_bundle() -> str:
    """certifi plus one intermediate the PUCT's server fails to send.

    puc.texas.gov chains through SSL.com's TLS Transit ECC CA R2, but serves the
    copy cross-signed by Comodo's AAA Certificate Services, a root Mozilla (and so
    certifi, and so most Linux images) dropped. Browsers paper over it by fetching
    the alternate path; requests will not. certs/ssl-com-tls-transit-ecc-r2.pem is
    that same CA as issued by SSL.com TLS ECC Root CA 2022, which certifi does
    trust, so the chain completes. Expires 2037-10-17.
    """
    global _BUNDLE
    if _BUNDLE is None:
        import atexit
        import os
        import tempfile

        import certifi
        extra = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "certs", "ssl-com-tls-transit-ecc-r2.pem")
        fd, path = tempfile.mkstemp(suffix="-ca.pem")
        with os.fdopen(fd, "w") as out:
            out.write(open(certifi.where()).read().rstrip() + "\n")
            out.write(open(extra).read().rstrip() + "\n")
        atexit.register(lambda: os.path.exists(path) and os.unlink(path))
        _BUNDLE = path
    return _BUNDLE


def _get(url: str) -> requests.Response:
    r = requests.get(url, headers=UA, timeout=60, verify=_ca_bundle())
    r.raise_for_status()
    return r


def _pdf_text(stem: str) -> str:
    raw = _get(f"{FTP_BASE}{stem}_Rate_Report.pdf").content
    reader = PdfReader(io.BytesIO(raw))
    return "\n".join(p.extract_text() or "" for p in reader.pages)


def _effective_date(text: str) -> date:
    m = re.search(r"(?:As of|Effective|Rates Report)\s*:?\s*"
                  r"([A-Z][a-z]+ \d{1,2},? \d{4}|\d{1,2}/\d{1,2}/\d{4})", text)
    if not m:
        raise ValueError("no effective date found in report")
    raw = m.group(1).replace(",", "")
    for fmt in ("%B %d %Y", "%m/%d/%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparsed effective date: {raw!r}")


def _residential_block(text: str) -> str:
    """Everything above the 500 kWh average-bill line.

    Residential is the first class in every one of these reports and the average-bill
    lines close it out, so the charge rows above that line are the residential ones.
    Class labels sit in merged cells that don't survive text extraction, so we anchor
    on the bill line instead. One of each charge row must be present, or we bail.
    """
    m = re.search(r"Average Residential Customer Bill \(500", text)
    if not m:
        raise ValueError("no 500 kWh average-bill line; report layout changed")
    block = text[: m.start()]
    for label in (r"Customer Charge", r"Metering\s+Charge", r"Volumetric Charge"):
        n = len(re.findall(label, block))
        if n != 1:
            raise ValueError(f"expected 1 {label!r} row above the residential "
                             f"average bill, found {n}; report layout changed")
    return block


def _amounts(label: str, hay: str, stem: str) -> list[float]:
    """Every dollar amount on the line a label appears on.

    The reports put the $ on either side of the number and pad with spaces, so
    reading the line and pulling the decimals out beats matching a money pattern.
    """
    m = re.search(label + r"[^\n]*", hay)
    if not m:
        raise ValueError(f"{stem}: no row for {label!r}; report layout changed")
    vals = [_f(v) for v in re.findall(r"[\d,]+\.\d+", m.group(0))]
    if not vals:
        raise ValueError(f"{stem}: no amount on the {label!r} row")
    return vals


def parse_pdf(csv_code: str) -> Rate:
    stem, _ = UTILITIES[csv_code]
    text = _pdf_text(stem)
    eff = _effective_date(text)
    block = _residential_block(text)
    # AEP files one report with two columns, Central then North.
    col = 1 if csv_code == "AEPNC" else 0
    cols = 2 if stem == "AEP" else 1

    def val(label: str, hay: str) -> float:
        vals = _amounts(label, hay, stem)
        if len(vals) != cols:
            raise ValueError(f"{stem}: expected {cols} amount(s) on the {label!r} "
                             f"row, got {vals}; report layout changed")
        return vals[col]

    monthly = val(r"Customer Charge", block) + val(r"Metering\s+Charge", block)
    per_kwh = val(r"Volumetric Charge", block)
    bill_1000 = val(r"Average Residential Customer Bill \(1,?000 kWh\)", text)

    return Rate(csv_code, round(monthly, 2), per_kwh, eff, bill_1000, f"{stem}_Rate_Report.pdf")


def parse_html() -> tuple[dict[str, dict[str, float]], date | None]:
    """Residential rows off the PUCT web table, keyed by CSV utility code."""
    soup = BeautifulSoup(_get(PAGE_URL).text, "html.parser")
    table = soup.find("table")
    if table is None:
        raise ValueError("no table on the PUCT rates page")

    rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            for tr in table.find_all("tr")]

    header = next((r for r in rows if any("Oncor" in c for c in r)), None)
    if not header:
        raise ValueError("no utility header row")
    col = {}
    for code, (_, label) in UTILITIES.items():
        idx = next((i for i, c in enumerate(header) if label.lower() in c.lower()), None)
        if idx is None:
            raise ValueError(f"utility column not found on page: {label}")
        col[code] = idx

    def row(pattern: str) -> list[str]:
        for r in rows:
            if r and re.search(pattern, r[0], re.I):
                return r
        raise ValueError(f"row not found on page: {pattern}")

    cust, meter = row(r"^Customer Charge"), row(r"^Metering Charge")
    vol, bill = row(r"^Volumetric"), row(r"1,?000 kWh")

    out = {}
    for code, i in col.items():
        cents = re.search(r"([\d.]+)\s*¢", vol[i])
        out[code] = {
            "monthly": round(_f(cust[i]) + _f(meter[i]), 2),
            "per_kwh": round(float(cents.group(1)) / 100, 8) if cents else _f(vol[i]),
            "bill_1000": _f(bill[i]),
        }

    m = re.search(r"As of ([A-Z][a-z]+ \d{1,2}, \d{4})", soup.get_text(" ", strip=True))
    eff = None
    if m:
        from datetime import datetime
        eff = datetime.strptime(m.group(1), "%B %d, %Y").date()
    return out, eff


def fetch_all(strict: bool = True) -> list[Rate]:
    html, html_eff = parse_html()
    rates, problems = [], []
    for code in UTILITIES:
        r = parse_pdf(code)
        h = html[code]

        if abs(h["monthly"] - r.monthly) > 0.005:
            problems.append(f"{code}: monthly PDF ${r.monthly} vs page ${h['monthly']}")
        if abs(h["per_kwh"] - r.per_kwh) > 1e-6:
            problems.append(f"{code}: per-kWh PDF {r.per_kwh} vs page {h['per_kwh']}")
        if html_eff and html_eff != r.effective:
            problems.append(f"{code}: effective PDF {r.effective} vs page {html_eff}")
        # PUCT's own average bill must reconcile with the charges it publishes,
        # allowing for a volumetric printed at low precision (AEP does this).
        if abs(r.monthly + 1000 * r.per_kwh - r.bill_1000) > 0.505:
            problems.append(
                f"{code}: ${r.bill_1000} avg bill doesn't reconcile with "
                f"${r.monthly} + 1000 x {r.per_kwh}")
        rates.append(r)

    if problems and strict:
        raise SystemExit("SOURCE DISAGREEMENT, refusing to write:\n  " + "\n  ".join(problems))
    for p in problems:
        print(f"WARN {p}", file=sys.stderr)
    return rates


# --- CSV ----------------------------------------------------------------

FIELDS = ["utility", "monthly", "perKwh", "startDate", "endDate"]


def _d(s: str) -> date:
    from datetime import datetime
    return datetime.strptime(s.strip(), "%m/%d/%Y").date()


def _s(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def season_end(start: date) -> date:
    """Texas TDU rates reset Mar 1 and Sep 1; rows run to the next boundary."""
    if 3 <= start.month <= 8:
        return date(start.year, 8, 31)
    year = start.year + 1 if start.month >= 9 else start.year
    return date(year, 2, calendar.monthrange(year, 2)[1])


def read_csv(path: str) -> list[dict]:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if rows and list(rows[0].keys()) != FIELDS:
        raise SystemExit(f"unexpected CSV columns: {list(rows[0].keys())}")
    return rows


def latest(rows: list[dict], utility: str) -> dict | None:
    hits = [r for r in rows if r["utility"] == utility]
    return max(hits, key=lambda r: _d(r["startDate"])) if hits else None


def plan_updates(rows: list[dict], rates: list[Rate], per_kwh_decimals: int = 6):
    """Rows to close and rows to append. Nothing changes if the rates match."""
    closes, adds, unchanged = [], [], []
    for r in rates:
        cur = latest(rows, r.utility)
        new_kwh = f"{r.per_kwh:.{per_kwh_decimals}f}".rstrip("0")
        new_monthly = f"{r.monthly:.2f}"
        if cur and abs(float(cur["perKwh"]) - r.per_kwh) < 1e-9 \
                and abs(float(cur["monthly"]) - r.monthly) < 0.005:
            unchanged.append((r, cur))
            continue
        if cur and _d(cur["startDate"]) >= r.effective:
            raise SystemExit(
                f"{r.utility}: existing row starts {cur['startDate']} but the PUCT "
                f"report is effective {_s(r.effective)}. Refusing to rewrite history.")
        if cur:
            closes.append((cur, _s(r.effective - timedelta(days=1))))
        adds.append({
            "utility": r.utility,
            "monthly": new_monthly,
            "perKwh": new_kwh,
            "startDate": _s(r.effective),
            "endDate": _s(season_end(r.effective)),
        })
    return closes, adds, unchanged


def apply_updates(path: str, closes, adds) -> None:
    """Edit in place at the line level so untouched rows stay byte-identical.

    The file is version-controlled and shipped, so the diff should show only the
    rows that actually changed - no reflowed line endings, no requoting.
    """
    with open(path, newline="") as f:
        lines = f.read().splitlines(keepends=True)
    term = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"

    for target, end in closes:
        old = ",".join(target[k] for k in FIELDS)
        hits = [i for i, ln in enumerate(lines) if ln.rstrip("\r\n") == old]
        if len(hits) != 1:
            raise SystemExit(f"expected 1 line matching {old!r}, found {len(hits)}")
        target = dict(target, endDate=end)
        lines[hits[0]] = ",".join(target[k] for k in FIELDS) + term

    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += term
    lines += [",".join(row[k] for k in FIELDS) + term for row in adds]

    with open(path, "w", newline="") as f:
        f.writelines(lines)


def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv", nargs="?", help="tdsp_charges.csv to check or update")
    p.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    p.add_argument("--no-strict", action="store_true",
                   help="warn instead of failing when the two PUCT sources disagree")
    a = p.parse_args(argv)

    rates = fetch_all(strict=not a.no_strict)
    print(f"PUCT residential TDU rates, effective {_s(rates[0].effective)}\n")
    for r in rates:
        note = ""
        # the average bill is published to the cent, so 1e-5 of slack is rounding,
        # not a rounded volumetric
        if abs(r.derived_per_kwh - r.per_kwh) > 1.1e-5:
            note = (f"   (published volumetric is rounded; the ${r.bill_1000} average bill "
                    f"implies {r.derived_per_kwh})")
        print(f"  {r.utility:6} monthly ${r.monthly:>5.2f}   perKwh {r.per_kwh:.6f}{note}")

    if not a.csv:
        return 0

    rows = read_csv(a.csv)
    closes, adds, unchanged = plan_updates(rows, rates)
    print()
    for r, cur in unchanged:
        print(f"  = {r.utility:6} unchanged ({cur['startDate']}-{cur['endDate']})")
    for cur, end in closes:
        print(f"  ~ close  {cur['utility']:6} {cur['startDate']}-{cur['endDate']} "
              f"-> endDate {end}  (was perKwh {cur['perKwh']})")
    for row in adds:
        print(f"  + add    {row['utility']:6} {row['monthly']} {row['perKwh']} "
              f"{row['startDate']}-{row['endDate']}")

    if not adds:
        print("\nNo change. CSV is current.")
        return 0
    if not a.apply:
        print(f"\n{len(adds)} row(s) to add. Dry run - rerun with --apply to write.")
        return 2
    apply_updates(a.csv, closes, adds)
    print(f"\nWrote {a.csv}: {len(closes)} closed, {len(adds)} added.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
