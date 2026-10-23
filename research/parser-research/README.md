# PriceWatch parser research

An experiment in universal extraction of product information from shop product pages.
It uses only `requests`, Beautiful Soup, and the Python standard library. There are no
site-specific selectors, browser automation, or retailer APIs.

## Run

```bash
python3 -m pip install -r requirements.txt
python3 parser_research.py
```

The script checks `robots.txt` before each product request. If access is explicitly
disallowed for its User-Agent, that URL is recorded as `ROBOTS_DENIED`. Network,
HTTP, and parser failures are isolated per URL.

`results.json` includes per-page diagnostics: discovered JSON-LD types, Product and
Offer counts, and every price candidate with its extraction source. `results.csv`
contains the flat summary fields.

Extraction priority is JSON-LD, Schema.org microdata, product metadata, then a
conservative DOM heuristic. Conflicting DOM prices produce `AMBIGUOUS` instead of a
guess. Prices are stored as `Decimal` until serialized.

## Experiment 2: requests vs Chromium

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
# Required by newer Playwright releases when Chromium is run headlessly:
python3 -m playwright install chromium-headless-shell
python3 experiment2.py
```

`experiment2.py` sends each URL through both fetchers and passes successful HTML to
the same `UniversalExtractor`. It writes `experiment2_results.json` (including
extraction diagnostics) and `experiment2_results.csv` (flat results). Fetch status
and parse status are intentionally separate.
