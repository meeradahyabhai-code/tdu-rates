# tdu-rates

One canonical copy of the Texas TDU delivery charges, refreshed from the PUCT on a
schedule, with a push to everything that consumes it. Replaces retyping rates off a
web page into a CSV once a month and hoping every project got the same numbers.

- `data/tdsp_charges.csv` — the canonical file. Same schema every project already uses.
- `data/tdu-rates.json` — the currently-effective rate per Texas TDU, for consumers
  that want today's number without parsing the history, and as the webhook payload.
- `puc_tdu.py` — the updater.
- `consumers/` — what a consuming project installs to subscribe.

## Flow

```
PUCT rate reports (PDF) ─┐
                         ├─ puc_tdu.py, cross-checks both ─→ data/tdsp_charges.csv
PUCT rates page (HTML) ──┘                                          │
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
python3 test_puc_tdu.py                            # 16 offline tests, no network
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

There is no review step, by choice. The gate is the cross-checking here: two
independent PUCT sources that must agree, reconciled against PUCT's own published
average bill, and nothing gets written when they don't. The realistic failure mode
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

Two independent PUCT sources, cross-checked against each other on every run:

| | |
|---|---|
| Rate report PDFs | `ftp.puc.texas.gov/public/puct-info/industry/electric/rates/tdr/tdu/{Oncor,CenterPoint,AEP,TNMP}_Rate_Report.pdf` — authoritative, carries the effective date, covers every rate class |
| Rates page | `puc.texas.gov/industry/electric/rates/tdr/` — residential only, used as a second opinion |

There is no PUCT API. The only JSON endpoint on the domain is
`/api/ercot/ercotstatus`, the grid-condition banner, not rates.

Mapping to the CSV columns:

- `monthly` = Customer Charge + Metering Charge
- `perKwh` = Volumetric Charge, as published
- `startDate` = the report's effective date
- `endDate` = the next seasonal boundary (Aug 31 or the last day of Feb; Texas TDU
  rates reset Mar 1 and Sep 1), and the previous row for that utility is closed the
  day before the new one starts

Covers the five Texas TDUs: `ONCOR`, `CNP`, `AEPCC`, `AEPNC`, `TNMP`. The other ~30
utilities in the file (Ohio, Illinois, Pennsylvania, plus `LUBBOCK`) are not on the
PUCT page and are never touched.

## What makes it refuse to write

A stale rate is recoverable; a wrong one silently misprices every bill check. So it
writes nothing and exits non-zero when:

- the PDF and the web page disagree on a charge or on the effective date
- PUCT's own published average 1,000 kWh bill doesn't reconcile with the charges on
  the same report
- a report's layout changed enough that a row is missing, duplicated, or has the
  wrong number of columns
- the effective date is on or before a row already in the CSV (that would mean
  rewriting history, not appending to it)

The scheduled job additionally re-runs the updater in dry-run mode afterward and
fails if the CSV still doesn't match the source, so a green run means the work
actually happened rather than merely that a command exited 0.

`--no-strict` downgrades the first check to a warning. Nothing downgrades the rest.

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
