"""Pull Texas residential TDU rates from PUCT and TXU and keep the CSV current.

Latest effective date wins; no source outranks another. Same-date disagreement
fails closed. PUCT reports are checked against its HTML page and average bills.
If TXU is unreachable, report the fallback and continue with PUCT alone.
"""

from __future__ import annotations

import calendar
import csv
import io
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

PAGE_URL = "https://www.puc.texas.gov/industry/electric/rates/tdr/"
FTP_BASE = "https://ftp.puc.texas.gov/public/puct-info/industry/electric/rates/tdr/tdu/"
TXU_PAGE_URL = "https://www.txu.com/help/billing-payments/tdu-charges"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) tdsp-rates/1.0"}

# CSV utility code -> (PDF file stem, label on the PUCT html table)
UTILITIES = {
    "ONCOR": ("Oncor", "Oncor"),
    "CNP": ("CenterPoint", "CenterPoint"),
    "AEPCC": ("AEP", "AEP Central"),
    "AEPNC": ("AEP", "AEP North"),
    "TNMP": ("TNMP", "Texas-New Mexico Power"),
}

TXU_HEADERS = (
    ("ONCOR", "ONCOR"),
    ("CNP", "CENTERPOINT ENERGY"),
    ("AEPCC", "AEP TEXAS CENTRAL"),
    ("AEPNC", "AEP TEXAS NORTH"),
    ("TNMP", "TEXAS - NEW MEXICO POWER"),
    ("LUBBOCK", "LUBBOCK POWER AND LIGHT"),
)


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
    # `strict` is retained for CLI/API compatibility; conflicts always fail closed.
    html, html_eff = parse_html()
    rates, problems = [], []
    for code in UTILITIES:
        r = parse_pdf(code)
        h = html[code]

        if html_eff is None or html_eff == r.effective:
            if not _same_charges(h["monthly"], h["per_kwh"], r):
                problems.append(f"{code}: same-date/undated PUCT page disagrees with PDF: "
                                f"${h['monthly']}/{h['per_kwh']} vs ${r.monthly}/{r.per_kwh}")
        # PUCT's own average bill must reconcile with the charges it publishes,
        # allowing for a volumetric printed at low precision (AEP does this).
        if abs(r.monthly + 1000 * r.per_kwh - r.bill_1000) > 0.505:
            problems.append(
                f"{code}: ${r.bill_1000} avg bill doesn't reconcile with "
                f"${r.monthly} + 1000 x {r.per_kwh}")
        rates.append(r)
        if html_eff is not None:
            if abs(h["monthly"] + 1000 * h["per_kwh"] - h["bill_1000"]) > 0.505:
                problems.append(f"{code}: PUCT page average bill doesn't reconcile")
            # Retain both dates until merge_sources has checked all conflicts.
            rates.append(Rate(code, h["monthly"], h["per_kwh"], html_eff,
                              h["bill_1000"], PAGE_URL))

    if problems:
        raise SystemExit("SOURCE DISAGREEMENT, refusing to write:\n  " + "\n  ".join(problems))
    return rates


# --- TXU charge sheet --------------------------------------------------

def _txu_pdf_url(page_html: str) -> str:
    soup = BeautifulSoup(page_html, "html.parser")
    hrefs = [a.get("href") for a in soup.find_all("a")
             if a.get("href") and "_RES_WEB_" in a.get("href").upper()
             and a.get("href").lower().split("?")[0].endswith(".pdf")]
    if len(hrefs) != 1:
        raise ValueError(f"TXU page: expected exactly one residential PDF link, found {len(hrefs)}")
    return urljoin(TXU_PAGE_URL, hrefs[0])


def _txu_header_names(text: str) -> list[str]:
    """Rebuild the six wrapped utility names using layout-mode column offsets."""
    title = re.search(r"^.*TDU Delivery Charges\s+\(Total Per Month & Total Per kWh by TDU\).*$",
                      text, re.I | re.M)
    ruler = re.search(r"^.*Total\s+TDU\s+Charges\s+Per\s+Month\s*:.*$", text, re.I | re.M)
    if not title or not ruler or ruler.start() <= title.end():
        raise ValueError("TXU table header: could not find title and column ruler")

    dollar_x = [ruler.start() - text.rfind("\n", 0, ruler.start()) - 1 + m.start()
                for m in re.finditer(r"\$\d[\d,]*\.\d+", ruler.group(0))]
    if len(dollar_x) != len(TXU_HEADERS):
        raise ValueError(f"TXU table header: expected 6 columns in ruler, got {len(dollar_x)}")

    after_title = text.find("\n", title.end())
    ends = [m.start() for pattern in (r"^Customer\s+Charge", r"^\s*TDU Delivery Charges Per Month:")
            if (m := re.search(pattern, text[after_title + 1:ruler.start()], re.I | re.M))]
    if not ends:
        raise ValueError("TXU table header: could not isolate header before value rows")
    header = text[after_title + 1:after_title + 1 + min(ends)]

    # The dollar sign is near the centre of each numeric cell, not its left edge.
    # Extrapolate the first edge by half the distance to the next column.
    first_edge = dollar_x[0] - (dollar_x[1] - dollar_x[0]) // 2
    boundaries = [first_edge] + [(a + b) // 2 for a, b in zip(dollar_x, dollar_x[1:])] + [10**9]
    columns: list[list[str]] = [[] for _ in TXU_HEADERS]
    for line in header.splitlines():
        for chunk in re.finditer(r"\S(?:.*?\S)?(?=\s{2,}|$)", line):
            if chunk.start() < boundaries[0]:
                continue
            overlaps = [max(0, min(chunk.end(), boundaries[i + 1])
                                - max(chunk.start(), boundaries[i]))
                        for i in range(len(TXU_HEADERS))]
            if max(overlaps):
                columns[overlaps.index(max(overlaps))].append(chunk.group())

    names = []
    for chunks in columns:
        name = re.sub(r"\s+", " ", " ".join(chunks).upper()).strip()
        name = re.sub(r"(?<=[A-Z])\d+(?:\s*,\s*\d+)*$", "", name).strip()
        names.append(name)
    return names


def _txu_header_order(text: str) -> list[str]:
    """Read and strictly validate utility names, never a fixed column index."""
    names = _txu_header_names(text)
    labels = [label for _, label in TXU_HEADERS]
    if names != labels:
        if sorted(names) == sorted(labels):
            raise ValueError(f"TXU table header: unexpected utility order {names!r}")
        missing = next((label for label in labels if label not in names), None)
        if missing:
            raise ValueError(f"TXU table header: expected utility {missing!r}")
        raise ValueError(f"TXU table header: unexpected utility sequence {names!r}")
    return [code for code, _ in TXU_HEADERS]


def _txu_row(text: str, label: str, count: int) -> list[float]:
    matches = re.findall(r"^[ \t]*" + label + r"[ \t]*:([^\n]+)", text, re.I | re.M)
    if len(matches) != 1:
        raise ValueError(f"TXU table: expected exactly one {label!r} row, got {len(matches)}")
    vals = [float(v.replace(",", "")) for v in re.findall(r"-?\d[\d,]*\.\d+", matches[0])]
    if len(vals) != count:
        raise ValueError(f"TXU table: expected {count} values on {label!r}, got {vals}")
    return vals


def parse_txu_text(text: str, source: str = "TXU residential charge sheet",
                   today: date | None = None) -> list[Rate]:
    """Validate every plausibly dated table, warning about far-future dates."""
    from datetime import datetime
    today = today if today is not None else date.today()
    latest = today + timedelta(days=120)
    titles = list(re.finditer(
        r"^.*TDU Delivery Charges\s+\(Total Per Month & Total Per kWh by TDU\).*$",
        text, re.I | re.M))
    date_pattern = r"Updated\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})"
    marks = list(re.finditer(date_pattern, text, re.I))
    if not marks:
        raise ValueError("TXU sheet: no Updated date found")
    if len(titles) != len(marks) or marks[0].start() < titles[0].start():
        raise ValueError("TXU sheet: expected one dated table per title; layout changed")
    rates = []
    for i, title in enumerate(titles):
        end = titles[i + 1].start() if i + 1 < len(titles) else len(text)
        table = text[title.start():end]
        dates = re.findall(date_pattern, table, re.I)
        if len(dates) != 1:
            raise ValueError("TXU table: expected exactly one Updated date")
        eff = datetime.strptime(re.sub(r"\s+", " ", dates[0]).title(), "%B %d, %Y").date()
        if eff > latest:
            print(f"WARN TXU table from {source}: skipping Updated {_s(eff)}; "
                  f"more than 120 days after today ({_s(today)}), likely a source date typo")
            continue
        order = _txu_header_order(table)
        monthly = _txu_row(table, r"Total\s+TDU\s+Charges\s+Per\s+Month", len(order))
        cents = _txu_row(table, r"Total\s+TDU\s+Charges\s+Per\s+kWh", len(order))
        rates.extend(Rate(code, round(monthly[j], 2), round(cents[j] / 100, 8), eff,
                          round(monthly[j] + 10 * cents[j], 2), source)
                     for j, code in enumerate(order))
    if not rates:
        raise ValueError(f"TXU sheet from {source}: no usable tables; all Updated dates are "
                         f"more than 120 days after today ({_s(today)}); refusing to use future rates")
    return rates


def fetch_txu() -> list[Rate]:
    page = _get(TXU_PAGE_URL).text
    pdf_url = _txu_pdf_url(page)
    reader = PdfReader(io.BytesIO(_get(pdf_url).content))
    # Layout mode keeps each table row together, which lets the parser verify that
    # exactly six values follow each total instead of borrowing from a later row.
    text = "\n".join(page.extract_text(extraction_mode="layout") or ""
                     for page in reader.pages)
    return parse_txu_text(text, pdf_url)


def merge_sources(puct: list[Rate], txu: list[Rate]) -> tuple[list[Rate], list[str]]:
    """Check all dates for conflicts, then select the newest rate per utility."""
    dated, origins = {}, {}
    for origin, candidates in (("PUCT", puct), ("TXU", txu)):
        for r in candidates:
            key = (r.utility, r.effective)
            previous = dated.get(key)
            if previous and not _same_charges(previous.monthly, previous.per_kwh, r):
                raise SystemExit(
                    f"SOURCE CONFLICT for {r.utility} on {_s(r.effective)}: "
                    f"{' + '.join(sorted(origins[key]))} ${previous.monthly:.2f}/{previous.per_kwh:.8f} vs "
                    f"{origin} ${r.monthly:.2f}/{r.per_kwh:.8f}; refusing to write")
            dated.setdefault(key, r)
            origins.setdefault(key, set()).add(origin)
    newest = {}
    for r in dated.values():
        if r.utility not in newest or r.effective > newest[r.utility].effective:
            newest[r.utility] = r
    accepted = list(newest.values())
    agreements = [r.utility for r in accepted if len(origins[(r.utility, r.effective)]) > 1]
    return accepted, agreements


def _same_charges(monthly: float, per_kwh: float, rate: Rate) -> bool:
    return abs(monthly - rate.monthly) < 0.005 and abs(per_kwh - rate.per_kwh) < 1e-9


def validate_txu_sanity(rates: list[Rate], rows: list[dict]) -> None:
    """Require at least one exact match to a rate already stored in the CSV."""
    for effective in {r.effective for r in rates}:
        table = [r for r in rates if r.effective == effective]
        if not any(row["utility"] == r.utility
                   and _same_charges(float(row["monthly"]), float(row["perKwh"]), r)
                   for r in table for row in rows):
            raise SystemExit(f"TXU SANITY CHECK FAILED on {_s(effective)}: "
                             "no utility matches any rate on file; refusing to write")


def validate_csv_sources(rates: list[Rate], rows: list[dict]) -> None:
    """A newer candidate must not hide a same-date conflict with stored history."""
    for r in rates:
        for row in rows:
            if (row["utility"] == r.utility and row["startDate"]
                    and _d(row["startDate"]) == r.effective
                    and not _same_charges(float(row["monthly"]), float(row["perKwh"]), r)):
                raise SystemExit(
                    f"SOURCE CONFLICT for {r.utility} on {_s(r.effective)}: "
                    f"CSV ${row['monthly']}/{row['perKwh']} vs {r.source} "
                    f"${r.monthly:.2f}/{r.per_kwh:.8f}; refusing to rewrite history")



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


def uncovered(rows: list[dict], today: date) -> list[str]:
    """Texas TDUs with no row in force on `today`. Must always be empty."""
    missing = []
    for utility in UTILITIES:
        covered = any(
            row["utility"] == utility
            and row["startDate"]
            and row["endDate"]
            and _d(row["startDate"]) <= today <= _d(row["endDate"])
            for row in rows
        )
        if not covered:
            missing.append(utility)
    return missing


def projected(rows, closes, adds, extends) -> list[dict]:
    """Return the CSV rows after applying a plan, without mutating `rows`."""
    end_dates = {id(row): end for row, end in [*closes, *extends]}
    result = [dict(row, endDate=end_dates.get(id(row), row["endDate"]))
              for row in rows]
    result.extend(dict(row) for row in adds)
    return result


def latest(rows: list[dict], utility: str) -> dict | None:
    hits = [r for r in rows if r["utility"] == utility]
    return max(hits, key=lambda r: _d(r["startDate"])) if hits else None


def _coverage_failure(rows: list[dict], today: date) -> SystemExit | None:
    """Explain any uncovered Texas TDUs in a form suitable for a hard failure."""
    missing = uncovered(rows, today)
    if not missing:
        return None
    details = []
    for utility in missing:
        dated = [row for row in rows if row["utility"] == utility
                 and row["startDate"] and row["endDate"]]
        if dated:
            newest = max(dated, key=lambda row: _d(row["startDate"]))
            details.append(f"{utility}: newest row ends {newest['endDate']}")
        else:
            details.append(f"{utility}: no dated rows")
    return SystemExit(
        "UNCOVERED TDU RATE(S), refusing to write:\n  "
        + "\n  ".join(details)
        + "\nThe rate could not be extended or appended from PUCT or TXU; "
        "a human has to look.")


def plan_updates(rows: list[dict], rates: list[Rate], per_kwh_decimals: int = 8):
    """Rows to close, append, or extend without duplicating an unchanged rate."""
    closes, adds, unchanged, ahead, extends = [], [], [], [], []
    for r in rates:
        cur = latest(rows, r.utility)
        new_kwh = f"{r.per_kwh:.{max(6, per_kwh_decimals)}f}".rstrip("0")
        new_kwh = new_kwh.ljust(new_kwh.index(".") + 7, "0")
        new_monthly = f"{r.monthly:.2f}"
        if cur and _d(cur["startDate"]) > r.effective:
            ahead.append((r, cur))
            continue
        if cur and abs(float(cur["perKwh"]) - r.per_kwh) < 1e-9 \
                and abs(float(cur["monthly"]) - r.monthly) < 0.005:
            end = _s(season_end(r.effective))
            if _d(cur["endDate"]) < _d(end):
                extends.append((cur, end))
            else:
                unchanged.append((r, cur))
            continue
        if cur and _d(cur["startDate"]) == r.effective:
            raise SystemExit(
                f"SOURCE CONFLICT for {r.utility}: existing row starts {cur['startDate']} "
                f"but {r.source!r} has different rates on {_s(r.effective)}. "
                "Refusing to rewrite history.")
        if cur:
            closes.append((cur, _s(r.effective - timedelta(days=1))))
        adds.append({
            "utility": r.utility,
            "monthly": new_monthly,
            "perKwh": new_kwh,
            "startDate": _s(r.effective),
            "endDate": _s(season_end(r.effective)),
        })
    return closes, adds, unchanged, ahead, extends


def apply_updates(path: str, closes, adds, extends=()) -> None:
    """Edit in place at the line level so untouched rows stay byte-identical.

    The file is version-controlled and shipped, so the diff should show only the
    rows that actually changed - no reflowed line endings, no requoting.
    """
    with open(path, newline="") as f:
        lines = f.read().splitlines(keepends=True)
    term = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"

    for target, end in [*closes, *extends]:
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
    p.add_argument("--today", help="MM/DD/YYYY, for coverage testing")
    p.add_argument("--no-strict", action="store_true",
                   help="deprecated compatibility flag; conflicts still fail closed")
    a = p.parse_args(argv)

    today = _d(a.today) if a.today else date.today()

    puct = fetch_all(strict=not a.no_strict)
    txu = []
    try:
        txu = fetch_txu()
    except requests.RequestException as exc:
        print(f"WARN TXU unreachable; falling back to PUCT alone: {exc}")

    # Conflict checking precedes newest-date selection, including older TXU tables.
    rates, agreements = merge_sources(puct, txu)
    rows = read_csv(a.csv) if a.csv else []
    validate_csv_sources(puct + txu, rows)
    if txu and rows:
        validate_txu_sanity(txu, rows)

    def origin(r):
        if r.utility in agreements:
            return "PUCT + TXU"
        return "TXU" if r in txu else "PUCT"

    print("Residential TDU rates (PUCT + TXU; latest effective date wins)\n")
    for r in rates:
        note = ""
        # the average bill is published to the cent, so 1e-5 of slack is rounding,
        # not a rounded volumetric
        if abs(r.derived_per_kwh - r.per_kwh) > 1.1e-5:
            note = (f"   (published volumetric is rounded; the ${r.bill_1000} average bill "
                    f"implies {r.derived_per_kwh})")
        print(f"  {r.utility:7} monthly ${r.monthly:>5.2f}   perKwh {r.per_kwh:.8f}  "
              f"effective {_s(r.effective)}  [{origin(r)}]{note}")

    if not a.csv:
        return 0

    closes, adds, unchanged, ahead, extends = plan_updates(rows, rates)
    print()
    for r, cur in unchanged:
        print(f"  = {r.utility:6} unchanged ({cur['startDate']}-{cur['endDate']}; {origin(r)})")
    for r, cur in ahead:
        print(f"  ! {r.utility:6} ahead: kept ours {cur['startDate']} monthly "
              f"${float(cur['monthly']):.2f} perKwh {float(cur['perKwh']):.6f}; "
              f"report says {_s(r.effective)} monthly ${r.monthly:.2f} "
              f"perKwh {r.per_kwh:.8f} [{origin(r)}]")
    for cur, end in closes:
        print(f"  ~ close  {cur['utility']:6} {cur['startDate']}-{cur['endDate']} "
              f"-> endDate {end}  (was perKwh {cur['perKwh']})")
    for cur, end in extends:
        rate = next(r for r in rates if r.utility == cur["utility"])
        print(f"  > extend {cur['utility']:6} {cur['startDate']}-{cur['endDate']} "
              f"-> endDate {end}  (rate unchanged at {float(cur['perKwh']):.6f}) [{origin(rate)}]")
    for row in adds:
        rate = next(r for r in rates if r.utility == row["utility"])
        print(f"  + add    {row['utility']:6} {row['monthly']} {row['perKwh']} "
              f"{row['startDate']}-{row['endDate']} [{origin(rate)}]")

    failure = _coverage_failure(projected(rows, closes, adds, extends), today)
    if failure:
        raise failure

    if not adds and not extends:
        if ahead:
            print("\nNo updates to apply. Kept the newer CSV row(s) shown above.")
        else:
            print("\nNo change. CSV is current.")
        return 0
    if not a.apply:
        print(f"\n{len(adds)} row(s) to add, {len(extends)} row(s) to extend. "
              "Dry run - rerun with --apply to write.")
        return 2
    apply_updates(a.csv, closes, adds, extends)
    failure = _coverage_failure(read_csv(a.csv), today)
    if failure:
        raise failure
    print(f"\nWrote {a.csv}: {len(closes)} closed, {len(adds)} added, "
          f"{len(extends)} extended.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (requests.RequestException, ValueError) as exc:
        print(f"SOURCE ERROR, refusing to write: {exc}", file=sys.stderr)
        raise SystemExit(1)
