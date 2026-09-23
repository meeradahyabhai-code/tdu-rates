"""Offline tests. No network - the parsing rules and the CSV edit rules only.

Run: python3 test_puc_tdu.py
"""

import os
import sys
import tempfile
from datetime import date

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


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("t_") and callable(f)]
    print(f"{len(tests)} tests")
    for name, fn in tests:
        check(name[2:], fn)
    print("\nFAILED: " + ", ".join(failures) if failures else "\nall passed")
    sys.exit(1 if failures else 0)
