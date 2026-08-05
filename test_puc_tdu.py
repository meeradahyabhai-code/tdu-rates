"""Offline tests. No network - the parsing rules and the CSV edit rules only.

Run: python3 test_puc_tdu.py
"""

import os
import sys
import tempfile
from datetime import date

import puc_tdu as m

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


def t_unchanged_is_a_noop():
    rates = [_rate("ONCOR", 4.06, 0.0611960, date(2026, 8, 1)),
             _rate("CNP", 4.90, 0.0514610, date(2026, 8, 1))]
    closes, adds, unchanged = m.plan_updates(_rows(), rates)
    eq((closes, adds), ([], []))
    eq(len(unchanged), 2)


def t_change_closes_and_appends():
    rates = [_rate("ONCOR", 4.06, 0.060295, date(2026, 8, 1))]
    closes, adds, _ = m.plan_updates(_rows(), rates)
    eq(len(closes), 1)
    eq(closes[0][0]["startDate"], "06/01/2026", "closes the newest row, not the oldest: ")
    eq(closes[0][1], "07/31/2026")
    eq(adds[0], {"utility": "ONCOR", "monthly": "4.06", "perKwh": "0.060295",
                 "startDate": "08/01/2026", "endDate": "08/31/2026"})


def t_monthly_only_change_still_counts():
    rates = [_rate("CNP", 5.10, 0.0514610, date(2026, 8, 1))]
    closes, adds, _ = m.plan_updates(_rows(), rates)
    eq(adds[0]["monthly"], "5.10")


def t_refuses_to_rewrite_history():
    # a report dated on or before a row we already have means something is off
    rates = [_rate("ONCOR", 4.06, 0.060295, date(2026, 6, 1))]
    raises(lambda: m.plan_updates(_rows(), rates), "refusing to rewrite history")


def t_apply_touches_only_changed_lines():
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        open(path, "w").write(CSV)
        rows = _rows()
        rates = [_rate("ONCOR", 4.06, 0.060295, date(2026, 8, 1))]
        closes, adds, _ = m.plan_updates(rows, rates)
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


def t_apply_is_idempotent():
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        open(path, "w").write(CSV)
        rates = [_rate("ONCOR", 4.06, 0.060295, date(2026, 8, 1))]
        closes, adds, _ = m.plan_updates(m.read_csv(path), rates)
        m.apply_updates(path, closes, adds)
        first = open(path).read()
        closes2, adds2, unchanged2 = m.plan_updates(m.read_csv(path), rates)
        eq((closes2, adds2), ([], []))
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
        closes, adds, _ = m.plan_updates(rows, rates)
        raises(lambda: m.apply_updates(path, closes, adds), "found 2")
    finally:
        os.unlink(path)


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
