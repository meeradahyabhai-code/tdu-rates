# Where the rates come from, and which one wins

Three sources publish Texas TDU delivery charges. They disagree routinely. This
file says how that is arbitrated and records what each source has actually done,
because the behaviour is not what you would guess and it is not stable.

## The rule

**The latest effective date wins. No source outranks another.**

| Source | Where | Character |
|---|---|---|
| **PUCT** | `puc.texas.gov/industry/electric/rates/tdr/` + the four rate-report PDFs | Authoritative record. Usually lags interim changes. |
| **TXU** | `txu.com/help/billing-payments/tdu-charges` -> dated PDF | Usually the current one. Carries one table per revision. |
| **MANUAL** | A sheet a supplier or Meera provides | Can be ahead of both. |

Two rules follow, and both matter:

1. **Sources disagreeing at *different* effective dates is the NORMAL state, not a
   defect.** One of them simply has not caught up. Never "resolve" it by picking a
   favourite source, and never fail a run over it.
2. **Sources disagreeing on the *same* effective date is a real conflict.** Fail
   closed, write nothing, exit non-zero. A wrong delivery rate silently misprices
   every bill check; a stale one is recoverable.

## Why it is a date rule and not a hierarchy

Meera, 2026-08-29, correcting an earlier design that had PUCT auto-apply and TXU
merely propose:

> the actual rates were the same as the txu update not the puc one that is slow,
> low tech and not updated. this produced the risk.

Verified on 924 bills, with a control:

| Cohort | n | Implied rate | Verdict |
|---|---|---|---|
| CNP, period ends before 8/15 | 272 | 0.051461 | both sources agree |
| CNP, period ends on/after 8/15 | 134 | **0.049811** | **TXU exactly. PUCT wrong.** |
| ONCOR, whole month | 518 | 0.060295 | both agree, and the bills agree with both |

The ONCOR row is the control: it rules out "TXU is just closer by luck."

But TXU won there **because it was fresher, not because it is better.** That
distinction is the whole design. Encode freshness, never a ranking — see the next
section for why that is not academic.

The bills were EVIDENCE for this rule. They are never an INPUT. This repo is
standalone and feeds bill-check, the published JSON and the consumer dispatch; it
must not depend on any consumer's data.

## Observed source behaviour

### 2026-08-28 — TXU ahead, PUCT lagging badly
CenterPoint cut delivery to 0.049811 effective 08/15/2026. TXU published it. **PUCT
was still showing 08/01/2026 at 0.051461 on 08/28** — thirteen days late. The daily
job logged "CNP unchanged / No change. CSV is current." and exited 0 every one of
those days.

**A green run is not evidence the rates are current.** Cost of that assumption: 49
Bill Audit customers emailed on a calculation a median $3.45 above their real bill.

### 2026-08-30 — a supplier ahead of both
A supplier sent the 09/01/2026 rates two days early, as a heads-up. Neither PUCT nor
TXU carried them. There was nothing to reconcile against and no supported way to
enter them. This is why MANUAL is a permanent path rather than a one-off.

### 2026-09-01 — **PUCT ahead of TXU. The order reversed.**
Measured this morning:

| | PUCT | TXU |
|---|---|---|
| Effective date carried | **09/01/2026** | 08/28/2026 |

PUCT published the September rates on time; TXU had not yet updated. **Anything that
had hardcoded "TXU is the current one" would have been wrong on this date.** The date
rule handled it with no change.

## Open conflict as of 2026-09-01

PUCT and the supplier sheet both claim effective **09/01/2026** and disagree on the
two AEP utilities. Four of six agree exactly:

| TDU | PUCT published | PUCT implied by its own avg bill | Supplier sheet | |
|---|---|---|---|---|
| ONCOR | 0.060295 | 0.060300 | 0.060295 | agree |
| CNP | 0.064130 | 0.064130 | 0.064130 | agree |
| TNMP | 0.074022 | 0.074020 | 0.074022 | agree |
| LUBBOCK | not carried | — | 0.063120 | TXU agrees (0.063120) |
| **AEPCC** | 0.058000 | 0.057830 | **0.057554** | **conflict** |
| **AEPNC** | 0.057000 | 0.056690 | **0.056407** | **conflict** |

This is **not** AEP's known rounding. AEP publishes three-decimal volumetrics while
others publish six, so the implied column exists to see through that — and the
implied values still do not match the supplier.

Note what the implied values are close to: TXU's 08/28 figures were AEPCC 0.057824
and AEPNC 0.056677. So PUCT's "09/01" AEP numbers look like the 08/28 rates
recarried under a new date, while the supplier's AEP numbers are different again.

**Unresolved. Do not assume either is right.** Cheapest ways to settle it, in order:
1. Wait for TXU to publish its 09/01 sheet and see which it matches.
2. Ask the supplier to confirm the two AEP figures.
3. Once September AEP bills exist, derive the rate from them:
   `(printed delivery - fixed monthly) / kWh`, full periods only, median per
   period-end date. A constant residual per kWh is a rate error; a fixed-charge error
   scales inversely with usage.

## An expired TDU is never allowed

Meera, 2026-09-04:

> THESE SHOULD NEVER BE EXPIRED. NEVER ALLOW EXPIRED TDU. IT EITHER NEEDS TO CHANGE THE
> END DATE IF THE VALUE DIDN'T CHANGE OR ADD A NEW ROW IF THE VALUE CHANGED.

Every one of the five Texas TDUs must have a row in force **today**, always. There are
exactly two ways to keep that true, and both are automatic:

| The PUCT report says | What happens |
|---|---|
| Same charges, newer effective date | **Extend** the existing row's `endDate` to the new season end. No duplicate row. |
| Different charges | **Close** the current row the day before, **append** a new one. |

If neither can be done, the job **fails and writes nothing**. An uncovered TDU is a hard
error, never a published state.

### Why this is a guardrail and not a field

`data/tdu-rates.json` has carried `"expired": true` and an `expiredUtilities` list since it
was built. Those keys stay — four consumer repos read them — but they can no longer be true,
because nothing gets published in that state.

ONCOR on 2026-09-01 is the case that forced this. PUCT filed its September report with Oncor
**unchanged** at 4.06 / 0.060295. `plan_updates()` classed it `unchanged`, wrote nothing, and
the August row lapsed on 08/31 with no successor. The daily job logged `= ONCOR unchanged`
and `No change. CSV is current.`, and exited **0** every day for three days. Downstream, every
Oncor bill with a period ending 09/01 or later was held `no_tariff`.

**The rate never changed. Only our row did.** This is the same class as the CenterPoint miss
on 08/28 and the `ahead` bug: a green run that is not evidence the data is good. The pattern
to distrust is any code path that treats "nothing to do" as "nothing is wrong."

### Where it is enforced

- `uncovered(rows, today)` in `puc_tdu.py` is the single definition of the invariant.
- `puc_tdu.py` checks the state the CSV is **about to be in**, so a dry run cannot pass while
  the plan leaves a hole, and re-reads the file after `--apply` to assert the outcome.
- `scripts/emit_json.py` refuses to build a payload with any expired utility.
- `.github/workflows/refresh.yml` asserts it before the commit step, so an uncovered TDU can
  never be committed or dispatched to consumers.

## What refuses to write

- The two published sources disagree on a charge or an effective date **for the same
  date**
- PUCT's own published average 1,000 kWh bill does not reconcile with the charges on
  the same report
- A report's layout changed enough that a row is missing, duplicated, or has the
  wrong column count
- The effective date is on or before a row already in the CSV — that is rewriting
  history, not appending to it
- No utility on a new TXU sheet matches any rate already on file (sanity check)
- Any Texas TDU would be left with no row in force today, and the report gives no way to
  extend or append one

`--no-strict` downgrades the first to a warning. Nothing downgrades the rest.

## The manual exception

Designed, **not yet built** as of 2026-09-01. The CLI today is `--apply` and
`--no-strict` only.

- `--manual <file>` plus a required `--manual-source "<who sent it, when>"`. It
  refuses to run without the attribution.
- The only path allowed past the rewrite-history guard and the same-date conflict
  gate, and only for the utilities its file names.
- Provenance goes to a sidecar `data/provenance.json`. **The CSV schema is frozen** at
  `utility,monthly,perKwh,startDate,endDate` — consumers verify that header, so a new
  column breaks all of them.
- **Reconcile-on-catch-up:** when a published source finally covers that effective
  date, it is compared against the manual row. Agree, confirm it. Disagree, fail
  loudly. An override nobody re-checks is unfalsifiable, and today's AEP conflict is
  exactly the case it exists to catch.
- A manual row no published source has confirmed shows in the daily run summary, and
  is marked UNCONFIRMED past 14 days. Visible, but it does not fail the job.

## Do not rename the CSV

`data/tdsp_charges.csv` is hardcoded in seven places, including the
`raw.githubusercontent.com` URL that bill-check, energy-xray, should-i-switch and
free-nights all pull. bill-check falls back to a stale local copy **silently**, so a
rename produces wrong prices rather than an error. Dated filenames belong in working
copies, never in this repo.
