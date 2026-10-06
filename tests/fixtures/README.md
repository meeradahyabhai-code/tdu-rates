# TXU layout fixtures

- `txu_20260828_res_layout.txt`: preserved verbatim from `txu-source-wip` via
  the supplied WIP diff. Contains August 28 and August 15, 2026 tables.
- `txu_20261004_res_layout.txt`: extracted from `/tmp/txu_oct04.pdf`, a local
  download timestamped October 6, 2026. Its companion `/tmp/txu.html` links to
  the PDF below. Contains October 4 and September 29, 2026 tables. The current
  sandbox could not independently refetch it (DNS unavailable; web fetch 403).

Discovery page: https://www.txu.com/help/billing-payments/tdu-charges

October PDF:
https://www.txu.com/-/media/Project/VistraApps/DT/Files/Content-Pages/Help-Center/20261004_-_TDU_Charges_Web_Update_RES_WEB_OCT04_2026_ENG_DETAIL.pdf

PDF SHA-256: `c2770da77663972af5bbc6fe8cf99183fece65fefdf9308f5b257f3c40ac72c0`

Extraction with pypdf 6.14.0:

```python
text = "\n".join(page.extract_text(extraction_mode="layout") or ""
                 for page in PdfReader(pdf_path).pages)
```

The layout text is unmodified. Tests assert all six monthly and per-kWh totals
for every table and reject swapped columns and missing values in either table.
