"""Offline tests. No network - the parsing rules and the CSV edit rules only.

Run: python3 test_puc_tdu.py
"""

import os
import sys
import tempfile
import io
import re
import subprocess
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import requests

import puc_tdu as m

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scripts import emit_json

ONCOR_PDF = """Class Charges Unit Current Charge
Customer Charge per Customer per Month 1.48 $
Metering Charge per Customer per Month 2.58 $
Volumetric Charges per kWh 0.060295 $
Average Residential Customer Bill (500 kWh) Per Month 34.21$
Average Residential Customer Bill (1,000 kWh) Per Month 64.36$
Average Residential Customer Bill (2,000 kWh) Per Month 124.65$
Customer Charge per Customer per Month 2.34 $
Metering Charge per Customer per Month 4.36 $
Volumetric Charges per kWh 0.051227 $
"""

AEP_PDF = """Central *North
Class Charges Unit Current Charge Current Charge
Customer Charge per Customer per Month 1.27 $                  1.27 $
Metering Charge per Meter per Month 1.97 $                  1.97 $
Volumetric Charge per kWh 0.057 $                0.055 $
Average Residential Customer Bill (500 kWh) Per Month 31.69$                30.89 $
Average Residential Customer Bill (1,000 kWh) Per Month 60.14$                58.55 $
"""

CSV = """utility,monthly,perKwh,startDate,endDate
ONCOR,4.23,0.056183,03/01/2026,05/31/2026
ONCOR,4.06,0.0611960,06/01/2026,08/31/2026
CNP,4.90,0.0514610,06/01/2026,08/31/2026
"""

COVERED_CSV = """utility,monthly,perKwh,startDate,endDate
ONCOR,4.06,0.0611960,06/01/2026,08/31/2026
CNP,4.90,0.0514610,06/01/2026,02/28/2027
AEPCC,3.24,0.057,06/01/2026,02/28/2027
AEPNC,3.24,0.055,06/01/2026,02/28/2027
TNMP,7.85,0.064665,06/01/2026,02/28/2027
"""

failures = []


def check(name, fn):
    try:
        # A missed mock must never turn the offline suite into a network test.
        with patch.object(m, "_get", side_effect=AssertionError("unexpected network access")), \
             patch.object(m, "fetch_txu", return_value=[]), redirect_stdout(io.StringIO()):
            fn()
        print(f"  ok   {name}")
    except (Exception, SystemExit) as e:  # SystemExit is how the module bails
        failures.append(name)
        print(f"  FAIL {name}: {type(e).__name__}: {e}")


def eq(a, b, what=""):
    assert a == b, f"{what}{a!r} != {b!r}"


def raises(fn, fragment):
    try:
        fn()
    except (Exception, SystemExit) as e:
        assert fragment.lower() in str(e).lower(), f"wrong error: {e}"
        return
    raise AssertionError(f"expected a failure mentioning {fragment!r}")


# --- parsing -----------------------------------------------------------

def t_block_stops_at_residential_bill():
    block = m._residential_block(ONCOR_PDF)
    eq(m._amounts(r"Volumetric Charge", block, "x"), [0.060295])
    assert "0.051227" not in block, "block leaked the Secondary class"


def t_block_needs_the_anchor():
    raises(lambda: m._residential_block(ONCOR_PDF.replace(
        "Average Residential Customer Bill (500", "Avg Res Bill (500")),
        "layout changed")


def t_block_rejects_extra_charge_rows():
    # a class inserted above Residential would silently poison the read
    poisoned = "Customer Charge per Customer per Month 9.99 $\n" + ONCOR_PDF
    raises(lambda: m._residential_block(poisoned), "found 2")


def t_amounts_two_columns():
    block = m._residential_block(AEP_PDF)
    eq(m._amounts(r"Volumetric Charge", block, "AEP"), [0.057, 0.055])
    eq(m._amounts(r"Customer Charge", block, "AEP"), [1.27, 1.27])


def t_amounts_ignores_thousands_in_labels():
    eq(m._amounts(r"Average Residential Customer Bill \(1,?000 kWh\)", AEP_PDF, "AEP"),
       [60.14, 58.55])


def t_amounts_missing_row():
    raises(lambda: m._amounts(r"Nonexistent Charge", ONCOR_PDF, "Oncor"), "no row")


def t_effective_date_formats():
    eq(m._effective_date("PUCT Monthly Report\nAs of August 1, 2026\n"), date(2026, 8, 1))
    eq(m._effective_date("(Effective August 1, 2026)"), date(2026, 8, 1))
    eq(m._effective_date("Rates Report 08/1/2026"), date(2026, 8, 1))
    raises(lambda: m._effective_date("no date here"), "no effective date")


# --- CSV rules ---------------------------------------------------------

def t_season_end():
    eq(m.season_end(date(2026, 3, 1)), date(2026, 8, 31))
    eq(m.season_end(date(2026, 8, 1)), date(2026, 8, 31))
    eq(m.season_end(date(2026, 9, 1)), date(2027, 2, 28))
    eq(m.season_end(date(2027, 9, 1)), date(2028, 2, 29), "leap year: ")
    eq(m.season_end(date(2027, 1, 15)), date(2027, 2, 28))


def _rows():
    import csv as _csv
    import io
    return list(_csv.DictReader(io.StringIO(CSV)))


def _rate(util, monthly, kwh, eff, bill=None):
    return m.Rate(util, monthly, kwh, eff, bill if bill is not None else monthly + 1000 * kwh,
                  "test")


def _covered_rows():
    import csv as _csv
    import io
    return list(_csv.DictReader(io.StringIO(COVERED_CSV)))


def _temp_csv(contents):
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    open(path, "w").write(contents)
    return path


def t_uncovered_is_empty_when_all_texas_tdus_are_covered():
    eq(m.uncovered(_covered_rows(), date(2026, 8, 1)), [])


def t_uncovered_reports_expired_oncor_only():
    eq(m.uncovered(_covered_rows(), date(2026, 9, 1)), ["ONCOR"])


def t_uncovered_ignores_undated_non_texas_rows():
    rows = _covered_rows()
    rows.append({"utility": "UGI", "monthly": "0", "perKwh": "0.05",
                 "startDate": "", "endDate": ""})
    eq(m.uncovered(rows, date(2026, 8, 1)), [])


def t_uncovered_reports_tdu_with_no_rows():
    rows = [row for row in _covered_rows() if row["utility"] != "TNMP"]
    eq(m.uncovered(rows, date(2026, 8, 1)), ["TNMP"])


def t_projected_applies_plan_without_mutating_rows():
    rows = _covered_rows()[:2]
    before = [dict(row) for row in rows]
    closes = [(rows[0], "07/31/2026")]
    extends = [(rows[1], "03/31/2027")]
    adds = [{"utility": "AEPCC", "monthly": "3.24", "perKwh": "0.057",
             "startDate": "08/01/2026", "endDate": "02/28/2027"}]
    result = m.projected(rows, closes, adds, extends)
    eq(rows, before, "projected mutated its input: ")
    eq(result[0]["endDate"], "07/31/2026")
    eq(result[1]["endDate"], "03/31/2027")
    eq(result[2], adds[0])


def t_unchanged_is_a_noop():
    rates = [_rate("ONCOR", 4.06, 0.0611960, date(2026, 8, 1)),
             _rate("CNP", 4.90, 0.0514610, date(2026, 8, 1))]
    closes, adds, unchanged, ahead, extends = m.plan_updates(_rows(), rates)
    eq((closes, adds), ([], []))
    eq(len(unchanged), 2)
    eq(ahead, [])
    eq(extends, [])


def t_change_closes_and_appends():
    rates = [_rate("ONCOR", 4.06, 0.060295, date(2026, 8, 1))]
    closes, adds, _, _, extends = m.plan_updates(_rows(), rates)
    eq(len(closes), 1)
    eq(closes[0][0]["startDate"], "06/01/2026", "closes the newest row, not the oldest: ")
    eq(closes[0][1], "07/31/2026")
    eq(adds[0], {"utility": "ONCOR", "monthly": "4.06", "perKwh": "0.060295",
                 "startDate": "08/01/2026", "endDate": "08/31/2026"})
    eq(extends, [])


def t_monthly_only_change_still_counts():
    rates = [_rate("CNP", 5.10, 0.0514610, date(2026, 8, 1))]
    closes, adds, _, _, extends = m.plan_updates(_rows(), rates)
    eq(adds[0]["monthly"], "5.10")
    eq(extends, [])


def t_refuses_to_rewrite_history():
    # A strictly older report is expected lag and must leave our newer row alone.
    rates = [_rate("ONCOR", 4.06, 0.060295, date(2026, 5, 1))]
    closes, adds, unchanged, ahead, extends = m.plan_updates(_rows(), rates)
    eq((closes, adds, unchanged), ([], [], []))
    eq(len(ahead), 1)
    eq(ahead[0][1]["startDate"], "06/01/2026")
    eq(extends, [])


def t_ahead_does_not_block_another_update():
    rates = [_rate("ONCOR", 4.06, 0.060295, date(2026, 5, 1)),
             _rate("CNP", 4.90, 0.049811, date(2026, 8, 1))]
    closes, adds, unchanged, ahead, extends = m.plan_updates(_rows(), rates)
    eq(len(ahead), 1)
    eq(ahead[0][0].utility, "ONCOR")
    eq(len(closes), 1)
    eq(adds[0]["utility"], "CNP")
    eq(extends, [])


def t_newer_report_updates_ahead_row_normally():
    rates = [_rate("ONCOR", 4.06, 0.060295, date(2026, 9, 1))]
    closes, adds, unchanged, ahead, extends = m.plan_updates(_rows(), rates)
    eq(ahead, [])
    eq(closes[0][1], "08/31/2026")
    eq(adds[0]["startDate"], "09/01/2026")
    eq(extends, [])


def t_unchanged_newer_effective_extends():
    rates = [_rate("ONCOR", 4.06, 0.0611960, date(2026, 9, 1))]
    closes, adds, unchanged, ahead, extends = m.plan_updates(_rows(), rates)
    eq((closes, adds, unchanged, ahead), ([], [], [], []))
    eq(extends, [(_rows()[1], "02/28/2027")])


def t_equal_season_end_is_unchanged():
    rates = [_rate("ONCOR", 4.06, 0.0611960, date(2026, 8, 1))]
    closes, adds, unchanged, ahead, extends = m.plan_updates(_rows(), rates)
    eq((closes, adds, ahead, extends), ([], [], [], []))
    eq(unchanged, [(rates[0], _rows()[1])])


def t_extend_never_shortens():
    rows = _rows()
    rows[1] = dict(rows[1], endDate="03/31/2027")
    rates = [_rate("ONCOR", 4.06, 0.0611960, date(2026, 9, 1))]
    closes, adds, unchanged, ahead, extends = m.plan_updates(rows, rates)
    eq((closes, adds, ahead, extends), ([], [], [], []))
    eq(unchanged, [(rates[0], rows[1])])


def t_changed_rate_does_not_extend():
    rates = [_rate("ONCOR", 4.06, 0.060295, date(2026, 9, 1))]
    closes, adds, unchanged, ahead, extends = m.plan_updates(_rows(), rates)
    eq(len(closes), 1)
    eq(len(adds), 1)
    eq((unchanged, ahead, extends), ([], [], []))


def t_main_exits_zero_for_ahead_only():
    path = _temp_csv(COVERED_CSV.replace("08/31/2026\nCNP", "02/28/2027\nCNP"))
    original_fetch = m.fetch_all
    try:
        m.fetch_all = lambda strict=True: [
            _rate("ONCOR", 4.06, 0.060295, date(2026, 5, 1))]
        before = open(path).read()
        eq(m.main(["--today", "09/01/2026", path]), 0)
        eq(open(path).read(), before)
    finally:
        m.fetch_all = original_fetch
        os.unlink(path)


def t_main_refuses_expired_tdu_when_plan_is_empty():
    path = _temp_csv(COVERED_CSV)
    original_fetch = m.fetch_all
    try:
        before = open(path).read()
        m.fetch_all = lambda strict=True: [
            _rate("ONCOR", 4.06, 0.0611960, date(2026, 8, 1))]
        raises(lambda: m.main(["--today", "09/01/2026", path]), "ONCOR")
        eq(open(path).read(), before, "coverage failure wrote the file: ")
    finally:
        m.fetch_all = original_fetch
        os.unlink(path)


def t_apply_touches_only_changed_lines():
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        open(path, "w").write(CSV)
        rows = _rows()
        rates = [_rate("ONCOR", 4.06, 0.060295, date(2026, 8, 1))]
        closes, adds, _, _, extends = m.plan_updates(rows, rates)
        eq(extends, [])
        m.apply_updates(path, closes, adds)
        before, after = CSV.splitlines(), open(path).read().splitlines()
        eq(after[:2], before[:2], "untouched lines rewritten: ")
        eq(after[2], "ONCOR,4.06,0.0611960,06/01/2026,07/31/2026")
        eq(after[3], before[3], "CNP row must not move or change: ")
        eq(after[4], "ONCOR,4.06,0.060295,08/01/2026,08/31/2026")
        eq(len(after), len(before) + 1)
        assert "\r" not in open(path, newline="").read(), "line endings changed"
    finally:
        os.unlink(path)


def t_apply_extends_only_without_duplicate_row():
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        open(path, "w").write(CSV)
        rates = [_rate("ONCOR", 4.06, 0.0611960, date(2026, 9, 1))]
        closes, adds, unchanged, ahead, extends = m.plan_updates(m.read_csv(path), rates)
        eq((closes, adds, unchanged, ahead), ([], [], [], []))
        before = open(path).read().splitlines()
        m.apply_updates(path, closes, adds, extends)
        after = open(path).read().splitlines()
        eq(after[:2], before[:2], "unrelated rows changed: ")
        eq(after[3:], before[3:], "unrelated rows changed: ")
        eq(after[2], "ONCOR,4.06,0.0611960,06/01/2026,02/28/2027")
        eq(len(after), len(before), "extend appended a duplicate row: ")
    finally:
        os.unlink(path)


def t_extend_apply_is_idempotent():
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        open(path, "w").write(CSV)
        rates = [_rate("ONCOR", 4.06, 0.0611960, date(2026, 9, 1))]
        closes, adds, _, _, extends = m.plan_updates(m.read_csv(path), rates)
        m.apply_updates(path, closes, adds, extends)
        first = open(path).read()
        closes2, adds2, unchanged2, ahead2, extends2 = m.plan_updates(m.read_csv(path), rates)
        eq((closes2, adds2, ahead2, extends2), ([], [], [], []))
        eq(len(unchanged2), 1)
        eq(open(path).read(), first)
    finally:
        os.unlink(path)


def t_apply_is_idempotent():
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        open(path, "w").write(CSV)
        rates = [_rate("ONCOR", 4.06, 0.060295, date(2026, 8, 1))]
        closes, adds, _, _, extends = m.plan_updates(m.read_csv(path), rates)
        eq(extends, [])
        m.apply_updates(path, closes, adds)
        first = open(path).read()
        closes2, adds2, unchanged2, ahead2, extends2 = m.plan_updates(m.read_csv(path), rates)
        eq((closes2, adds2), ([], []))
        eq(ahead2, [])
        eq(extends2, [])
        eq(open(path).read(), first)
    finally:
        os.unlink(path)


def t_apply_bails_on_ambiguous_line():
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        open(path, "w").write(CSV + "ONCOR,4.06,0.0611960,06/01/2026,08/31/2026\n")
        rows = m.read_csv(path)
        rates = [_rate("ONCOR", 4.06, 0.060295, date(2026, 8, 1))]
        closes, adds, _, _, extends = m.plan_updates(rows, rates)
        eq(extends, [])
        raises(lambda: m.apply_updates(path, closes, adds), "found 2")
    finally:
        os.unlink(path)


def t_main_dry_run_and_apply_extends_only():
    path = _temp_csv(COVERED_CSV)
    original_fetch = m.fetch_all
    try:
        m.fetch_all = lambda strict=True: [
            _rate("ONCOR", 4.06, 0.0611960, date(2026, 9, 1))]
        before = open(path).read()
        eq(m.main(["--today", "09/01/2026", path]), 2)
        eq(open(path).read(), before, "dry run wrote the file: ")
        eq(m.main(["--today", "09/01/2026", "--apply", path]), 0)
        oncor = next(line for line in open(path).read().splitlines()
                     if line.startswith("ONCOR,"))
        eq(oncor, "ONCOR,4.06,0.0611960,06/01/2026,02/28/2027")
        eq(m.uncovered(m.read_csv(path), date(2026, 9, 1)), [])
    finally:
        m.fetch_all = original_fetch
        os.unlink(path)


def t_main_rechecks_coverage_after_apply():
    path = _temp_csv(COVERED_CSV)
    original_fetch = m.fetch_all
    original_apply = m.apply_updates
    try:
        before = open(path).read()
        m.fetch_all = lambda strict=True: [
            _rate("ONCOR", 4.06, 0.0611960, date(2026, 9, 1))]
        m.apply_updates = lambda path, closes, adds, extends: None
        raises(lambda: m.main(["--apply", path, "--today", "09/01/2026"]), "ONCOR")
        eq(open(path).read(), before, "no-op writer changed the file: ")
    finally:
        m.fetch_all = original_fetch
        m.apply_updates = original_apply
        os.unlink(path)


def t_emit_json_rejects_expired_rates_and_preserves_contract_when_covered():
    expired = _temp_csv(COVERED_CSV)
    covered = _temp_csv(COVERED_CSV.replace("08/31/2026", "02/28/2027", 1))
    try:
        try:
            emit_json.build(expired, date(2026, 9, 1))
        except SystemExit as e:
            assert "ONCOR" in str(e) and "08/31/2026" in str(e), f"wrong error: {e}"
        else:
            raise AssertionError("expected expired CSV to be rejected")
        payload = emit_json.build(covered, date(2026, 9, 1))
        eq(payload["expiredUtilities"], [])
        assert all(not utility["expired"] for utility in payload["utilities"].values())
    finally:
        os.unlink(expired)
        os.unlink(covered)


def t_derived_per_kwh_flags_rounding():
    r = _rate("AEPCC", 3.24, 0.057, date(2026, 8, 1), bill=60.14)
    eq(r.derived_per_kwh, 0.0569)
    exact = _rate("TNMP", 7.85, 0.064665, date(2026, 8, 1), bill=72.52)
    assert abs(exact.derived_per_kwh - exact.per_kwh) < 1.1e-5, "false rounding flag"


# --- TXU layout fixtures and source arbitration -------------------------

FIXTURES = Path(__file__).parent / "tests/fixtures"
TXU_TEXT = (FIXTURES / "txu_20260828_res_layout.txt").read_text()
TXU_OCT_TEXT = (FIXTURES / "txu_20261004_res_layout.txt").read_text()
FETCH_TXU = m.fetch_txu


def _assert_txu_tables(text, expected):
    rates = m.parse_txu_text(text)
    eq(len(rates), 6 * len(expected))
    eq({r.effective for r in rates}, set(expected))
    for effective, values in expected.items():
        table = [r for r in rates if r.effective == effective]
        eq([r.utility for r in table], ["ONCOR", "CNP", "AEPCC", "AEPNC", "TNMP", "LUBBOCK"])
        eq([(r.monthly, r.per_kwh) for r in table], values, str(effective))


def t_txu_august_all_six_values_in_both_tables():
    _assert_txu_tables(TXU_TEXT, {
        date(2026, 8, 28): [(4.06, .060295), (4.90, .049811), (3.24, .057824),
                            (3.24, .056677), (7.85, .064665), (0, .063120)],
        date(2026, 8, 15): [(4.06, .060295), (4.90, .049811), (3.24, .058272),
                            (3.24, .056677), (7.85, .064665), (0, .063120)],
    })


def t_txu_october_all_six_values_in_both_tables():
    _assert_txu_tables(TXU_OCT_TEXT, {
        date(2026, 10, 4): [(4.06, .068673), (4.90, .064130), (3.24, .056898),
                            (3.24, .055751), (7.56, .077710), (0, .063120)],
        date(2026, 9, 29): [(4.06, .060295), (4.90, .064130), (3.24, .056898),
                            (3.24, .055751), (7.56, .077710), (0, .063120)],
    })


def t_txu_far_future_table_is_skipped_with_warning():
    text = TXU_OCT_TEXT.replace("Updated September 29, 2026", "Updated September 30, 2029")
    source = "https://www.txu.com/-/media/corrected_RES_WEB_OCT04_2026.pdf"
    today = date(2026, 10, 8)
    out = io.StringIO()
    with redirect_stdout(out):
        rates = m.parse_txu_text(text, source, today=today)
    expected = [r for r in m.parse_txu_text(TXU_OCT_TEXT, source, today=today)
                if r.effective == date(2026, 10, 4)]
    eq(rates, expected)
    eq(len(rates), 6)
    warning = out.getvalue()
    for fragment in ("WARN", "skipping Updated 09/30/2029", source, "120 days", "10/08/2026"):
        assert fragment in warning, warning


def t_txu_all_future_tables_explain_failure():
    text = TXU_OCT_TEXT.replace("2026", "2029")
    source = "https://www.txu.com/-/media/future_RES_WEB.pdf"
    out = io.StringIO()
    with redirect_stdout(out):
        raises(lambda: m.parse_txu_text(text, source, today=date(2026, 10, 8)),
               f"TXU sheet from {source}: no usable tables; all Updated dates are "
               "more than 120 days after today (10/08/2026)")
    eq(out.getvalue().count("WARN TXU table"), 2)


def t_txu_future_cutoff_allows_exactly_120_days():
    effective = date(2026, 10, 4)
    out = io.StringIO()
    with redirect_stdout(out):
        rates = m.parse_txu_text(TXU_OCT_TEXT, today=effective - timedelta(days=120))
    eq(len(rates), 12)
    eq(out.getvalue(), "")
    with redirect_stdout(out):
        rates = m.parse_txu_text(TXU_OCT_TEXT, today=effective - timedelta(days=121))
    eq(len(rates), 6)
    eq({r.effective for r in rates}, {date(2026, 9, 29)})
    assert "skipping Updated 10/04/2026" in out.getvalue()


def t_txu_column_swap_is_rejected_including_older_table():
    for text in (TXU_TEXT, TXU_OCT_TEXT):
        lines = text.splitlines(keepends=True)
        titles = [i for i, line in enumerate(lines) if "(Total Per Month" in line]
        for title_i in titles:
            swapped = lines.copy()
            end_i = next(i for i in range(title_i, len(lines)) if "Total TDU Charges Per Month:" in lines[i])
            dollars = [x.start() for x in re.finditer(r"\$", lines[end_i])]
            left = dollars[0] - (dollars[1] - dollars[0]) // 2
            middle, right = (dollars[0] + dollars[1]) // 2, (dollars[1] + dollars[2]) // 2
            # Move complete wrapped header cells, preserving the numeric ruler.
            header_end = next(i for i in range(title_i + 1, end_i)
                              if "TDU Delivery Charges Per Month:" in lines[i])
            for i in range(title_i + 1, header_end):
                raw = lines[i].rstrip("\n").ljust(right)
                first, second = raw[left:middle].strip(), raw[middle:right].strip()
                swapped[i] = raw[:left] + second.center(middle-left) + first.center(right-middle) + raw[right:] + "\n"
            raises(lambda: m.parse_txu_text("".join(swapped)), "unexpected utility order")
    raises(lambda: m.parse_txu_text(TXU_TEXT.replace("AEP TEXAS", "AEP      ", 1)), "expected utility")


def t_txu_six_value_guard_and_missing_older_rows():
    for text in (TXU_TEXT, TXU_OCT_TEXT):
        for label in ("Total TDU Charges Per Month:", "Total TDU Charges Per kWh:"):
            lines = text.splitlines()
            indices = [i for i, line in enumerate(lines) if label in line]
            for i in indices:
                bad = lines.copy()
                bad[i] = re.sub(r"\s+\S+\s*$", "", bad[i])
                raises(lambda: m.parse_txu_text("\n".join(bad)), "expected 6")
                bad = lines.copy()
                del bad[i]
                raises(lambda: m.parse_txu_text("\n".join(bad)), "row" if "kWh" in label else "ruler")


def t_txu_pdf_discovery_and_layout_extraction():
    from types import SimpleNamespace
    from unittest.mock import Mock
    url = "https://www.txu.com/-/media/changed_RES_WEB_OCT04_2026.pdf"
    html = '<a href="/-/media/changed_RES_WEB_OCT04_2026.pdf">rates</a>'
    eq(m._txu_pdf_url(html), url)
    raises(lambda: m._txu_pdf_url("<p>missing</p>"), "exactly one")
    raises(lambda: m._txu_pdf_url(html + html.replace("changed", "another")), "exactly one")
    page = Mock()
    page.extract_text.return_value = TXU_OCT_TEXT
    with patch.object(m, "_get", side_effect=[SimpleNamespace(text=html), SimpleNamespace(content=b"pdf")]) as get, \
         patch.object(m, "PdfReader", return_value=SimpleNamespace(pages=[page])):
        rates = FETCH_TXU()
    eq([c.args[0] for c in get.call_args_list], [m.TXU_PAGE_URL, url])
    page.extract_text.assert_called_once_with(extraction_mode="layout")
    eq(len(rates), 12)
    assert all(r.source == url for r in rates)


def t_source_newest_wins_both_directions_and_input_orders():
    puct = [_rate("CNP", 4.90, .065333, date(2026, 10, 1)),
            _rate("ONCOR", 4.06, .070000, date(2026, 10, 5))]
    txu = m.parse_txu_text(TXU_OCT_TEXT)
    for candidates in (txu, list(reversed(txu))):
        merged, _ = m.merge_sources(puct, candidates)
        chosen = {r.utility: r for r in merged}
        eq(chosen["CNP"].per_kwh, .064130)
        eq(chosen["CNP"].effective, date(2026, 10, 4))
        eq(chosen["ONCOR"], puct[1])


def t_same_date_conflict_in_older_table_is_not_hidden():
    puct = [_rate("CNP", 4.90, .065333, date(2026, 9, 29))]
    raises(lambda: m.merge_sources(puct, m.parse_txu_text(TXU_OCT_TEXT)), "SOURCE CONFLICT")
    rates = m.parse_txu_text(TXU_OCT_TEXT)
    raises(lambda: m.merge_sources([], rates + [_rate("ONCOR", 99, .068673, date(2026, 10, 4))]), "SOURCE CONFLICT")


def t_equal_date_equal_values_report_both_sources():
    rate = _rate("CNP", 4.90, .064130, date(2026, 10, 4))
    merged, agreements = m.merge_sources([rate], [rate])
    eq(merged, [rate])
    eq(agreements, ["CNP"])


def t_csv_same_date_conflict_cannot_hide_behind_newer_candidate():
    rows = _rows()
    old = _rate("ONCOR", 4.23, .099999, date(2026, 3, 1))
    new = _rate("ONCOR", 4.06, .068673, date(2026, 10, 4))
    raises(lambda: m.validate_csv_sources([old, new], rows), "SOURCE CONFLICT")
    old.per_kwh = .056183
    m.validate_csv_sources([old, new], rows)


def t_same_date_conflict_cli_exits_nonzero_and_writes_nothing():
    path = _temp_csv(COVERED_CSV)
    try:
        before = Path(path).read_bytes()
        script = '''
import sys
from datetime import date
from unittest.mock import patch
import puc_tdu as m
r = m.Rate("CNP", 4.90, .065333, date(2026, 10, 4), 70.233, "PUCT")
t = m.Rate("CNP", 4.90, .064130, date(2026, 10, 4), 69.030, "TXU")
with patch.object(m, "fetch_all", return_value=[r]), patch.object(m, "fetch_txu", return_value=[t]):
    raise SystemExit(m.main([sys.argv[1], "--no-strict"]))
'''
        result = subprocess.run([sys.executable, "-c", script, path], cwd=Path(__file__).parent,
                                capture_output=True, text=True)
        eq(result.returncode, 1)
        assert "SOURCE CONFLICT" in result.stderr, result.stderr
        eq(Path(path).read_bytes(), before)
    finally:
        os.unlink(path)


def t_txu_unreachable_falls_back_visibly():
    path = _temp_csv(COVERED_CSV.replace("08/31/2026", "02/28/2027", 1))
    try:
        for error in (requests.Timeout("timeout"), requests.ConnectionError("offline"), requests.HTTPError("503")):
            out = io.StringIO()
            with patch.object(m, "fetch_all", return_value=[_rate("ONCOR", 4.06, .061196, date(2026, 9, 1))]), \
                 patch.object(m, "fetch_txu", side_effect=error), redirect_stdout(out):
                eq(m.main([path, "--today", "10/06/2026"]), 0)
            assert "TXU unreachable; falling back to PUCT alone" in out.getvalue()
        eq(Path(path).read_text(), COVERED_CSV.replace("08/31/2026", "02/28/2027", 1))
    finally:
        os.unlink(path)


def t_txu_parse_failure_does_not_fall_back():
    with patch.object(m, "fetch_all", return_value=[]), \
         patch.object(m, "fetch_txu", side_effect=ValueError("layout changed")):
        raises(lambda: m.main([]), "layout changed")


def t_txu_sanity_checks_each_table():
    good = _rate("ONCOR", 4.06, .061196, date(2026, 8, 28))
    m.validate_txu_sanity([good], _rows())
    bad = _rate("ONCOR", 99.99, .999999, date(2026, 8, 15))
    raises(lambda: m.validate_txu_sanity([good, bad], _rows()), "sanity check failed")


def t_october_fixture_plan_preserves_coverage_and_csv_history():
    import csv
    rows = list(csv.DictReader(io.StringIO("""utility,monthly,perKwh,startDate,endDate
ONCOR,4.06,0.060295,08/01/2026,02/28/2027
CNP,4.90,0.065333,10/01/2026,02/28/2027
AEPCC,3.24,0.057,10/01/2026,02/28/2027
AEPNC,3.24,0.056,10/01/2026,02/28/2027
TNMP,7.56,0.07771,10/01/2026,02/28/2027
LUBBOCK,0.00,0.063120,09/01/2025,08/31/2026
""")))
    # Freeze the October 6 CSV state so this test survives future refreshes.
    puct = [_rate(code, float(cur["monthly"]), float(cur["perKwh"]), m._d(cur["startDate"]))
            for code in m.UTILITIES for cur in [m.latest(rows, code)]]
    txu = m.parse_txu_text(TXU_OCT_TEXT)
    merged, _ = m.merge_sources(puct, txu)
    m.validate_txu_sanity(txu, rows)
    closes, adds, _, _, extends = m.plan_updates(rows, merged)
    by_code = {r["utility"]: r for r in adds}
    eq(by_code["ONCOR"]["startDate"], "10/04/2026")
    eq(by_code["ONCOR"]["perKwh"], "0.068673")
    eq(by_code["CNP"]["startDate"], "10/04/2026")
    eq(by_code["CNP"]["perKwh"], "0.064130")
    assert all(end == "10/03/2026" for _, end in closes)
    eq(extends, [(m.latest(rows, "LUBBOCK"), "02/28/2027")])
    after = m.projected(rows, closes, adds, extends)
    eq(m.uncovered(after, date(2026, 10, 6)), [])
    # The verification step must be a no-op against the merged result.
    closes2, adds2, _, ahead2, extends2 = m.plan_updates(after, merged)
    eq((closes2, adds2, ahead2, extends2), ([], [], [], []))
    contents = ",".join(m.FIELDS) + "\n" + "\n".join(",".join(row[k] for k in m.FIELDS) for row in rows) + "\n"
    path = _temp_csv(contents)
    try:
        out = io.StringIO()
        with patch.object(m, "fetch_all", return_value=puct), \
             patch.object(m, "fetch_txu", return_value=txu), redirect_stdout(out):
            eq(m.main([path, "--today", "10/06/2026"]), 2)
        additions = [line for line in out.getvalue().splitlines() if line.startswith("  + add")]
        eq(len(additions), 4)
        assert all(line.endswith("[TXU]") for line in additions)
        eq(Path(path).read_text(), contents)
    finally:
        os.unlink(path)


def t_txu_precision_is_not_rounded_to_six_decimals():
    rate = _rate("ONCOR", 4.06, .06867345, date(2026, 10, 4))
    _, adds, _, _, _ = m.plan_updates(_rows(), [rate])
    eq(adds[0]["perKwh"], "0.06867345")


def t_puct_different_dates_are_normal_but_same_date_conflicts_are_fatal():
    r = _rate("ONCOR", 4.06, .060295, date(2026, 10, 1))
    html = {"ONCOR": {"monthly": 4.06, "per_kwh": .068673, "bill_1000": 72.733}}
    with patch.object(m, "UTILITIES", {"ONCOR": m.UTILITIES["ONCOR"]}), \
         patch.object(m, "parse_pdf", return_value=r):
        with patch.object(m, "parse_html", return_value=(html, date(2026, 10, 4))):
            merged, _ = m.merge_sources(m.fetch_all(), [])
            eq(merged[0].per_kwh, .068673)
        with patch.object(m, "parse_html", return_value=(html, r.effective)):
            raises(lambda: m.fetch_all(strict=False), "SOURCE DISAGREEMENT")
        html["ONCOR"]["bill_1000"] = 999
        with patch.object(m, "parse_html", return_value=(html, date(2026, 10, 4))):
            raises(lambda: m.fetch_all(strict=False), "doesn't reconcile")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("t_") and callable(f)]
    print(f"{len(tests)} tests")
    for name, fn in tests:
        check(name[2:], fn)
    print("\nFAILED: " + ", ".join(failures) if failures else "\nall passed")
    sys.exit(1 if failures else 0)
