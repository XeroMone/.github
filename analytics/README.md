# Roblox Analytics Collector

Hourly collector for the XeroMone Roblox analytics warehouse.

It reads Roblox's official Analytics Query API catalog, queries each enabled experience, and upserts the results into the **Roblox Analytics Warehouse** Google Sheet.

## What it collects

- Every metric currently listed by Roblox's official Analytics Query API documentation.
- The total series for every metric.
- Every supported dimension as an individual breakdown by default.
- Optional pairwise dimension breakdowns when `Breakdown Depth` is set to `2` in the Sheet.
- Hourly buckets whenever Roblox supports them. Daily metrics are refreshed hourly so mutable current-day and retention values can settle.
- Roblox performance metrics at `OneHour` by default rather than `OneMinute`, because minute-level data across every metric and dimension would overwhelm a Google Sheet quickly.

The collector synchronizes the **Metrics Catalog** tab from Roblox creator-docs, while preserving per-metric `Enabled` switches.

## Google Sheet

Spreadsheet ID:

`1gZw4kP2RpjEywWCsEeaT5cDRguMBu8ICHJ_jqyyxkKM`

The important tabs are:

- `Games`: experiences to collect.
- `Metrics Catalog`: official metrics and supported dimensions.
- `Raw Analytics`: normalized/upserted raw observations.
- `Settings`: collection behavior.
- `Collector Log`: every scheduled run and its result.

### Add an experience

In `Games`, fill at least:

| Game | Universe ID | Primary Place ID | Analytics Enabled |
| --- | --- | --- | --- |
| Spleef | `123...` | `456...` | `TRUE` |

`Universe ID` is required. `Primary Place ID` is useful metadata but not required by the Analytics Query API.

## Required GitHub Actions secrets

### `ROBLOX_OPEN_CLOUD_API_KEY`

Create or update a Roblox Open Cloud API key and give it access to each experience you want to collect with:

- system: `universe-analytics`
- operation: `universe.analytics:read`

Never commit the key.

### `GOOGLE_SERVICE_ACCOUNT_JSON`

1. Create a Google Cloud project.
2. Enable the Google Sheets API.
3. Create a service account and JSON key.
4. Share the Roblox Analytics Warehouse Sheet with the service account's `client_email` as **Editor**.
5. Put the complete JSON key into the GitHub Actions secret `GOOGLE_SERVICE_ACCOUNT_JSON`.

Never commit the JSON key.

## Schedule

`.github/workflows/collect-roblox-analytics.yml` runs at minute 7 of every hour and can also be started manually from GitHub Actions.

## Deduplication

`Raw Analytics!S:S` contains a deterministic `Record Key` derived from:

- universe
- metric
- native granularity
- bucket timestamp
- dimension names and values

When Roblox revises a recent bucket, the collector updates the existing row instead of appending another copy.

## Collection depth

`Settings > Breakdown Depth` controls dimensional expansion:

- `0`: totals only
- `1`: totals plus each supported dimension individually
- `2`: also pairwise dimension combinations

Depth `1` is the default because it captures the full single-dimension Creator Dashboard slicing surface without creating a combinatorial explosion. Depth `2` can become extremely large for metrics with many dimensions.

## First run

The first successful run for an experience uses `Initial Backfill Days` from the Settings tab. Subsequent hourly runs use smaller rolling windows and upsert them so late-arriving data and retention cohorts can mature without duplicating rows.
