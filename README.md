# tdu-rates

One canonical copy of the Texas TDU delivery charges, refreshed from PUCT and TXU on a
schedule, with a push to everything that consumes it. Replaces retyping rates off a
web page into a CSV once a month and hoping every project got the same numbers.

- `data/tdsp_charges.csv` — the canonical file. Same schema every project already uses.
- `data/tdu-rates.json` — the currently-effective rate per Texas TDU, for consumers
  that want today's number without parsing the history, and as the webhook payload.
- `puc_tdu.py` — the updater.
- `consumers/` — what a consuming project installs to subscribe.

## Flow

```
PUCT reports + HTML ─────┐
                         ├─ puc_tdu.py, latest date wins ─→ data/tdsp_charges.csv
TXU charge sheet (PDF) ──┘                                          │
                                                                    │ commit
                                                     ┌──────────────┴──────────────┐
                                                     │  daily GitHub Actions job   │
                                                     └──────────────┬──────────────┘
                                                                    │
                            ┌───────────────────────────────────────┼───────────────────────┐
                            ▼                                       ▼                       ▼
                  repository_dispatch                        Slack webhook          outgoing webhook
                  → energy-xray  (commit → deploy)           (optional)             (optional, JSON body)
                  → should-i-switch (commit → deploy)
                  → bill-check: ./sync-tdu.sh (local, not on GitHub)
```

A rate change lands on main in each project and deploys itself. Everything that
protects that is upstream of the commit, in the cross-checks below.

## Using it

```bash
python3 puc_tdu.py data/tdsp_charges.csv           # dry run, prints the plan
python3 puc_tdu.py data/tdsp_charges.csv --apply   # writes
python3 scripts/emit_json.py                       # rebuild data/tdu-rates.json
python3 test_puc_tdu.py                            # offline tests, no network
```

Exit codes: `0` nothing to do or applied, `2` changes pending in a dry run, `1` a
source looked wrong and nothing was written.

## Subscribing a project

**On GitHub** — copy `consumers/sync-tdu-rates.yml` into `.github/workflows/`. It
fires on the dispatch from here, on a daily cron as a backstop, and by hand. It
verifies what it downloaded before touching anything: right header, at least 100
rows, all five TDUs present, never shorter than the file it's replacing. Then it
commits straight to main, which is what makes the new rate deploy on its own, and
re-reads main afterwards to confirm the file actually landed.

There is no review step, by choice. The gate is the cross-checking here: PUCT and
TXU must agree for the same effective date, and PUCT's charges must reconcile with
its own published average bill. Different dates are normal; the latest wins.
Nothing gets written on a same-date conflict. The realistic failure mode
without this job isn't a wrong rate, it's a stale one, which is what four months of
hand-updating produced.

**Not on GitHub** — `consumers/sync-tdu.sh ~/bill-check/data/tdsp_charges.csv`. Same
checks, writes in place, prints the diff, idempotent.

To make the push side work, this repo needs:

| Secret / variable | Purpose | Without it |
|---|---|---|
| `CONSUMER_DISPATCH_TOKEN` (secret) | PAT with `contents: write` on the consumer repos | consumers still pick it up on their daily cron |
| `CONSUMER_REPOS` (variable) | space-separated `owner/repo` list | same |
| `SLACK_WEBHOOK_URL` (secret) | Slack ping on change | no Slack ping |
| `RATES_WEBHOOK_URL` (secret) | POSTs `tdu-rates.json` anywhere | no outgoing webhook |

Each is optional and skipped when unset. With none of them, the commit here plus the
consumers' daily cron still keeps everything in sync within a day.

## Where the numbers come from

The daily job reads **PUCT AND TXU**. The latest effective date wins per utility;
no source outranks another. See [SOURCES.md](SOURCES.md) for the contract.

| | |
|---|---|
| Rate report PDFs | `ftp.puc.texas.gov/public/puct-info/industry/electric/rates/tdr/tdu/{Oncor,CenterPoint,AEP,TNMP}_Rate_Report.pdf` — authoritative, carries the effective date, covers every rate class |
| Rates page | `puc.texas.gov/industry/electric/rates/tdr/` — residential only, used as a second opinion |
| TXU charge sheet | `txu.com/help/billing-payments/tdu-charges` — discovers the current residential PDF link on each run; parses every dated table in layout mode |

Every TXU table must have the six expected utility columns and six values per
total row. Cents are converted to dollars per kWh with at least six decimals.
Conflicts are checked across all dates before choosing each utility's newest rate.
TXU connection, timeout, or HTTP failures fall back to PUCT with a visible warning
in the output and daily summary. Invalid TXU content fails closed.

There is no PUCT API. The only JSON endpoint on the domain is
`/api/ercot/ercotstatus`, the grid-condition banner, not rates.

Mapping to the CSV columns:

- `monthly` = Customer Charge + Metering Charge
- `perKwh` = Volumetric Charge, as published
- `startDate` = the report's effective date
- `endDate` = the next seasonal boundary (Aug 31 or the last day of Feb; Texas TDU
  rates reset Mar 1 and Sep 1), and the previous row for that utility is closed the
  day before the new one starts

Covers `ONCOR`, `CNP`, `AEPCC`, `AEPNC`, and `TNMP` through both sources, plus
`LUBBOCK` through TXU. The other ~30 utilities (Ohio, Illinois, Pennsylvania) are
never touched. Bills are never an input.

## What makes it refuse to write

A stale rate is recoverable; a wrong one silently misprices every bill check. So it
writes nothing and exits non-zero when:

- sources disagree on charges for the same effective date (different dates are normal)
- PUCT's own published average 1,000 kWh bill doesn't reconcile with the charges on
  the same report
- a report's layout changed enough that a row is missing, duplicated, or has the
  wrong number of columns
- an existing CSV row has different charges for the same effective date
- no utility in a TXU table matches any rate already on file
- the proposed CSV would leave any of the five PUCT TDUs uncovered today

The scheduled job additionally re-runs the updater in dry-run mode afterward and
fails if the CSV still doesn't match the merged PUCT + TXU result, so a green run means the work
actually happened rather than merely that a command exited 0.

`--no-strict` is retained as a deprecated compatibility flag; it cannot bypass
conflicts or validation. Unchanged charges with a newer effective date extend the
existing row to the season end; changed charges close the old row at newDate−1
and append a new row. A CSV row newer than the sources is kept and reported.

## Known quirk: AEP publishes a rounded volumetric

AEP reports `0.057` / `0.055` where the other TDUs report six decimals. Their own
average-bill figures imply `0.05690` and `0.055310`. The CSV gets the published
number, matching what's already in the file, and the run prints the implied value
next to it. On 1,000 kWh the gap is about a dime a month. Say the word and it can
write the implied figure instead.

## TLS note

`certs/ssl-com-tls-transit-ecc-r2.pem` is in the repo because puc.texas.gov serves an
intermediate signed by a root Mozilla dropped, so plain `requests` (and most Linux
images) can't build a path. That file is the same CA as signed by a root certifi does
trust. It expires 2037-10-17. Without it, fetches fail closed with a certificate
error — they never silently skip verification.
