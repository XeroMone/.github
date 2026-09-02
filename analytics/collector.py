from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import gspread
import requests
from google.oauth2.service_account import Credentials

ROBLOX_API_ROOT = "https://apis.roblox.com/analytics-query-api"
ROBLOX_METRICS_DOC = (
    "https://raw.githubusercontent.com/Roblox/creator-docs/main/"
    "content/en-us/cloud/guides/analytics/metrics.md"
)
DEFAULT_SHEET_ID = "1gZw4kP2RpjEywWCsEeaT5cDRguMBu8ICHJ_jqyyxkKM"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


@dataclass(frozen=True)
class MetricSpec:
    category: str
    name: str
    metric: str
    granularities: tuple[str, ...]
    retention_days: int | None
    dimensions: tuple[str, ...]


@dataclass
class RunStats:
    games: int = 0
    metrics: int = 0
    queries: int = 0
    rows_added: int = 0
    rows_updated: int = 0
    errors: int = 0


class RobloxQueryError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"true", "1", "yes", "y", "on"}


def parse_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def build_google_client() -> gspread.Client:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from exc
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def fetch_metric_catalog(session: requests.Session) -> list[MetricSpec]:
    response = session.get(ROBLOX_METRICS_DOC, timeout=30)
    response.raise_for_status()
    text = response.text

    specs: list[MetricSpec] = []
    category = ""
    in_supported_metrics = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "## Supported metrics":
            in_supported_metrics = True
            continue
        if in_supported_metrics and line.startswith("## ") and line != "## Supported metrics":
            break
        if not in_supported_metrics:
            continue
        if line.startswith("### "):
            category = line[4:].strip()
            continue
        if not category or not line.startswith("|"):
            continue
        if line.startswith("| ---") or line.startswith("| Name |"):
            continue

        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        name_cell, metric_cell, dims_cell = cells[:3]
        metric_codes = re.findall(r"<code>([^<]+)</code>", metric_cell)
        if not metric_codes:
            continue
        gran_match = re.search(r"\*\*Granularities:\*\*\s*([^<]+?)(?:<br|$)", metric_cell)
        if not gran_match:
            continue
        granularities = tuple(
            part.strip() for part in gran_match.group(1).split(",") if part.strip()
        )
        retention_match = re.search(r"\*\*Data retention:\*\*\s*(\d+)\s*days", metric_cell)
        retention_days = int(retention_match.group(1)) if retention_match else None
        dimensions = tuple(re.findall(r"<code>([^<]+)</code>", dims_cell))
        display_name = re.sub(r"<[^>]+>", "", name_cell).strip()

        for metric_code in metric_codes:
            specs.append(
                MetricSpec(
                    category=category,
                    name=display_name,
                    metric=metric_code,
                    granularities=granularities,
                    retention_days=retention_days,
                    dimensions=dimensions,
                )
            )

    if len(specs) < 20:
        raise RuntimeError(
            f"Roblox metrics catalog parser returned only {len(specs)} metrics; refusing to continue"
        )
    return specs


def load_settings(sheet: gspread.Spreadsheet) -> dict[str, str]:
    ws = sheet.worksheet("Settings")
    values = ws.get_all_values()
    settings: dict[str, str] = {}
    for row in values[1:]:
        if not row or not row[0].strip():
            continue
        settings[row[0].strip()] = row[1].strip() if len(row) > 1 else ""
    return settings


def load_games(sheet: gspread.Spreadsheet) -> list[dict[str, Any]]:
    ws = sheet.worksheet("Games")
    values = ws.get_all_values()
    if not values:
        return []
    header = values[0]
    index = {name: i for i, name in enumerate(header)}
    games: list[dict[str, Any]] = []

    for row_number, row in enumerate(values[1:], start=2):
        def cell(name: str) -> str:
            i = index.get(name)
            return row[i].strip() if i is not None and i < len(row) else ""

        universe_id = cell("Universe ID")
        if not universe_id:
            continue
        if not universe_id.isdigit():
            print(f"Skipping Games row {row_number}: Universe ID is not numeric", file=sys.stderr)
            continue
        enabled = parse_bool(cell("Analytics Enabled"), default=True)
        if not enabled:
            continue
        games.append(
            {
                "row": row_number,
                "name": cell("Game") or f"Universe {universe_id}",
                "universe_id": universe_id,
                "place_id": cell("Primary Place ID"),
                "last_fetch": cell("Last Fetch"),
            }
        )
    return games


def existing_metric_enabled_map(ws: gspread.Worksheet) -> dict[str, bool]:
    values = ws.get_all_values()
    if not values:
        return {}
    header = values[0]
    try:
        metric_i = header.index("Metric")
        enabled_i = header.index("Enabled")
    except ValueError:
        return {}
    result: dict[str, bool] = {}
    for row in values[1:]:
        if metric_i < len(row) and row[metric_i].strip():
            enabled_value = row[enabled_i] if enabled_i < len(row) else "TRUE"
            result[row[metric_i].strip()] = parse_bool(enabled_value, default=True)
    return result


def preferred_granularity(spec: MetricSpec, settings: dict[str, str]) -> str:
    supported = spec.granularities
    performance_setting = settings.get("Performance Granularity", "OneHour") or "OneHour"
    if spec.category.lower().startswith("performance") and performance_setting in supported:
        return performance_setting
    for candidate in ("OneHour", "OneDay", "OneWeek", "OneMonth", "None"):
        if candidate in supported:
            return candidate
    return supported[0]


def sync_metric_catalog(
    sheet: gspread.Spreadsheet,
    specs: list[MetricSpec],
    settings: dict[str, str],
) -> dict[str, bool]:
    ws = sheet.worksheet("Metrics Catalog")
    prior_enabled = existing_metric_enabled_map(ws)
    header = [
        "Category",
        "Metric",
        "Display Name",
        "API / Source",
        "Preferred Granularity",
        "Supported Granularities",
        "Dimensions",
        "Poll Frequency",
        "Enabled",
        "Retention Window",
        "Notes",
    ]
    rows = [header]
    for spec in specs:
        granularity = preferred_granularity(spec, settings)
        enabled = prior_enabled.get(spec.metric, True)
        retention = f"{spec.retention_days} days" if spec.retention_days else ""
        rows.append(
            [
                spec.category,
                spec.metric,
                spec.name,
                "Analytics Query API",
                granularity,
                ", ".join(spec.granularities),
                ", ".join(spec.dimensions),
                "Hourly collector",
                enabled,
                retention,
                "Catalog synchronized from Roblox creator-docs.",
            ]
        )
    ws.clear()
    ws.update(rows, "A1", value_input_option="USER_ENTERED")
    return {spec.metric: prior_enabled.get(spec.metric, True) for spec in specs}


def load_metric_enabled_map(sheet: gspread.Spreadsheet) -> dict[str, bool]:
    return existing_metric_enabled_map(sheet.worksheet("Metrics Catalog"))


def query_window(
    spec: MetricSpec,
    granularity: str,
    settings: dict[str, str],
    first_run: bool,
    now: datetime,
) -> tuple[datetime, datetime]:
    initial_days = parse_int(settings.get("Initial Backfill Days"), 90)
    if spec.retention_days:
        initial_days = min(initial_days, spec.retention_days)

    if granularity == "OneHour":
        if first_run:
            start = now - timedelta(days=initial_days)
        else:
            hours = parse_int(settings.get("Hourly Lookback Hours"), 72)
            start = now - timedelta(hours=hours)
        return start.replace(minute=0, second=0, microsecond=0), now

    if granularity == "OneDay":
        if first_run:
            days = initial_days
        elif spec.metric == "ForwardD1Retention":
            days = parse_int(settings.get("D1 Lookback Days"), 4)
        elif spec.metric == "ForwardD7Retention":
            days = parse_int(settings.get("D7 Lookback Days"), 10)
        elif spec.metric == "ForwardD30Retention" or spec.metric == "DailyCohortRetention":
            days = parse_int(settings.get("D30 Lookback Days"), 35)
        else:
            days = parse_int(settings.get("Daily Lookback Days"), 3)
        start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now

    if granularity == "OneWeek":
        days = initial_days if first_run else max(14, parse_int(settings.get("Daily Lookback Days"), 3) * 7)
        return now - timedelta(days=days), now

    if granularity == "OneMonth":
        days = initial_days if first_run else 62
        return now - timedelta(days=days), now

    none_days = initial_days if first_run else parse_int(settings.get("None Window Days"), 30)
    return now - timedelta(days=none_days), now


def request_roblox(
    session: requests.Session,
    api_key: str,
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 5,
) -> dict[str, Any]:
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    for attempt in range(max_attempts):
        try:
            if method == "POST":
                response = session.post(url, headers=headers, json=payload, timeout=60)
            else:
                response = session.get(url, headers=headers, timeout=60)
        except requests.RequestException as exc:
            if attempt + 1 == max_attempts:
                raise RobloxQueryError(str(exc)) from exc
            time.sleep(min(2**attempt, 16))
            continue

        if response.status_code == 429 or response.status_code >= 500:
            if attempt + 1 == max_attempts:
                raise RobloxQueryError(f"HTTP {response.status_code}: {response.text[:500]}")
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else min(2**attempt, 16)
            except ValueError:
                delay = min(2**attempt, 16)
            time.sleep(delay)
            continue

        if response.status_code >= 400:
            raise RobloxQueryError(f"HTTP {response.status_code}: {response.text[:1000]}")
        try:
            return response.json()
        except ValueError as exc:
            raise RobloxQueryError("Roblox returned non-JSON data") from exc

    raise RobloxQueryError("Roblox request exhausted retries")


def query_metric(
    session: requests.Session,
    api_key: str,
    universe_id: str,
    spec: MetricSpec,
    granularity: str,
    start: datetime,
    end: datetime,
    breakdown: tuple[str, ...],
) -> dict[str, Any]:
    url = f"{ROBLOX_API_ROOT}/v1/universes/{universe_id}/metrics"
    payload: dict[str, Any] = {
        "metric": spec.metric,
        "granularity": granularity,
        "startTime": iso(start),
        "endTime": iso(end),
    }
    if breakdown:
        payload["breakdown"] = list(breakdown)

    envelope = request_roblox(session, api_key, "POST", url, payload=payload)
    deadline = time.time() + 90
    while not envelope.get("done", False):
        if time.time() >= deadline:
            raise RobloxQueryError("Timed out waiting for long-running analytics query")
        path = str(envelope.get("path", "")).lstrip("/")
        if not path:
            raise RobloxQueryError("Long-running query did not return an operation path")
        time.sleep(2)
        envelope = request_roblox(
            session,
            api_key,
            "GET",
            f"{ROBLOX_API_ROOT}/{path}",
        )

    if envelope.get("error"):
        error = envelope["error"]
        raise RobloxQueryError(f"Roblox query error {error.get('code')}: {error.get('message')}")
    return envelope.get("response", {})


def breakdown_sets(spec: MetricSpec, depth: int, max_pairs: int) -> list[tuple[str, ...]]:
    result: list[tuple[str, ...]] = [tuple()]
    if depth >= 1:
        result.extend((dimension,) for dimension in spec.dimensions)
    if depth >= 2 and len(spec.dimensions) >= 2:
        pairs = list(itertools.combinations(spec.dimensions, 2))
        if max_pairs > 0:
            pairs = pairs[:max_pairs]
        result.extend(pairs)
    return result


def make_record_key(
    universe_id: str,
    metric: str,
    granularity: str,
    timestamp: str,
    breakdowns: list[dict[str, Any]],
    query_start: str = "",
    query_end: str = "",
) -> str:
    normalized_breakdowns = sorted(
        (
            str(item.get("dimension", "")),
            str(item.get("value", "")),
        )
        for item in breakdowns
    )
    payload = [
        universe_id,
        metric,
        granularity,
        timestamp,
        normalized_breakdowns,
        query_start if granularity == "None" else "",
        query_end if granularity == "None" else "",
    ]
    return hashlib.sha1(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()


def normalize_response(
    game: dict[str, Any],
    spec: MetricSpec,
    granularity: str,
    response: dict[str, Any],
    fetched_at: str,
    query_start: str,
    query_end: str,
) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for series in response.get("values", []) or []:
        breakdowns = series.get("breakdowns", []) or []
        dims = []
        for item in breakdowns[:3]:
            dims.extend([item.get("dimension", ""), item.get("displayValue") or item.get("value", "")])
        while len(dims) < 6:
            dims.append("")

        for point in series.get("dataPoints", []) or []:
            timestamp = point.get("time") or query_start
            numeric_value = point.get("value", "")
            string_values = point.get("stringValues")
            if string_values is not None and numeric_value in (None, ""):
                numeric_value = json.dumps(string_values, ensure_ascii=False)
            notes_payload = {"displayName": spec.name}
            if point.get("status") is not None:
                notes_payload["status"] = point.get("status")
            if granularity == "None":
                notes_payload["windowStart"] = query_start
                notes_payload["windowEnd"] = query_end
            notes = json.dumps(notes_payload, ensure_ascii=False, separators=(",", ":"))
            record_key = make_record_key(
                game["universe_id"],
                spec.metric,
                granularity,
                str(timestamp),
                breakdowns,
                query_start,
                query_end,
            )
            rows.append(
                [
                    timestamp,
                    game["name"],
                    game["universe_id"],
                    game.get("place_id", ""),
                    spec.category,
                    spec.metric,
                    granularity,
                    numeric_value,
                    "",
                    dims[0],
                    dims[1],
                    dims[2],
                    dims[3],
                    dims[4],
                    dims[5],
                    "Analytics Query API",
                    fetched_at,
                    notes,
                    record_key,
                ]
            )
    return rows


def upsert_raw_rows(ws: gspread.Worksheet, rows: list[list[Any]]) -> tuple[int, int]:
    if not rows:
        return 0, 0

    existing_keys = ws.col_values(19)
    key_to_row: dict[str, int] = {}
    for row_number, key in enumerate(existing_keys[1:], start=2):
        if key:
            key_to_row[key] = row_number

    new_rows: list[list[Any]] = []
    updates: list[dict[str, Any]] = []
    for row in rows:
        key = str(row[18])
        existing_row = key_to_row.get(key)
        if existing_row:
            updates.append(
                {
                    "range": f"H{existing_row}:R{existing_row}",
                    "values": [[row[i] for i in range(7, 18)]],
                }
            )
        else:
            new_rows.append(row)

    for batch in chunks(updates, 500):
        ws.batch_update(batch, value_input_option="USER_ENTERED")
    for batch in chunks(new_rows, 500):
        ws.append_rows(batch, value_input_option="USER_ENTERED", insert_data_option="INSERT_ROWS")

    return len(new_rows), len(updates)


def update_game_last_fetch(sheet: gspread.Spreadsheet, game_rows: list[int], fetched_at: str) -> None:
    if not game_rows:
        return
    ws = sheet.worksheet("Games")
    updates = [{"range": f"J{row}", "values": [[fetched_at]]} for row in game_rows]
    ws.batch_update(updates, value_input_option="USER_ENTERED")


def append_log(
    sheet: gspread.Spreadsheet,
    started_at: datetime,
    status: str,
    stats: RunStats,
    notes: str,
) -> None:
    duration = round((utc_now() - started_at).total_seconds(), 2)
    sheet.worksheet("Collector Log").append_row(
        [
            iso(utc_now()),
            status,
            stats.games,
            stats.metrics,
            stats.queries,
            stats.rows_added,
            stats.rows_updated,
            stats.errors,
            duration,
            notes,
        ],
        value_input_option="USER_ENTERED",
    )


def run() -> int:
    parser = argparse.ArgumentParser(description="Collect Roblox Analytics Query API data into Google Sheets")
    parser.add_argument("--dry-run", action="store_true", help="Query Roblox but do not write Raw Analytics")
    args = parser.parse_args()

    started_at = utc_now()
    stats = RunStats()
    api_key = os.environ.get("ROBLOX_OPEN_CLOUD_API_KEY", "").strip()
    if not api_key:
        print("ROBLOX_OPEN_CLOUD_API_KEY is not set", file=sys.stderr)
        return 2

    sheet_id = os.environ.get("GOOGLE_SHEET_ID", DEFAULT_SHEET_ID).strip() or DEFAULT_SHEET_ID
    session = requests.Session()
    session.headers.update({"User-Agent": "XeroMone-Roblox-Analytics-Collector/1.0"})

    try:
        google = build_google_client()
        sheet = google.open_by_key(sheet_id)
        settings = load_settings(sheet)
        if not parse_bool(settings.get("Collector Enabled"), default=True):
            append_log(sheet, started_at, "SKIPPED", stats, "Collector Enabled is FALSE")
            return 0

        specs = fetch_metric_catalog(session)
        if parse_bool(settings.get("Sync Metrics Catalog"), default=True):
            enabled_map = sync_metric_catalog(sheet, specs, settings)
        else:
            enabled_map = load_metric_enabled_map(sheet)

        games = load_games(sheet)
        stats.games = len(games)
        if not games:
            append_log(sheet, started_at, "SKIPPED", stats, "No enabled Universe IDs in Games tab")
            print("No enabled games found in the Games tab.")
            return 0

        depth = max(0, min(2, parse_int(settings.get("Breakdown Depth"), 1)))
        max_pairs = max(0, parse_int(settings.get("Max Pairwise Breakdowns"), 12))
        raw_ws = sheet.worksheet("Raw Analytics")
        successful_game_rows: list[int] = []
        errors: list[str] = []
        now = utc_now()

        for game in games:
            first_run = not bool(game.get("last_fetch"))
            game_had_success = False
            for spec in specs:
                if not enabled_map.get(spec.metric, True):
                    continue
                stats.metrics += 1
                granularity = preferred_granularity(spec, settings)
                start, end = query_window(spec, granularity, settings, first_run, now)
                query_start = iso(start)
                query_end = iso(end)

                for breakdown in breakdown_sets(spec, depth, max_pairs):
                    try:
                        stats.queries += 1
                        response = query_metric(
                            session,
                            api_key,
                            game["universe_id"],
                            spec,
                            granularity,
                            start,
                            end,
                            breakdown,
                        )
                        normalized = normalize_response(
                            game,
                            spec,
                            granularity,
                            response,
                            iso(utc_now()),
                            query_start,
                            query_end,
                        )
                        if args.dry_run:
                            if normalized:
                                game_had_success = True
                            continue
                        added, updated = upsert_raw_rows(raw_ws, normalized)
                        stats.rows_added += added
                        stats.rows_updated += updated
                        game_had_success = True
                    except RobloxQueryError as exc:
                        stats.errors += 1
                        descriptor = f"{game['name']} / {spec.metric} / {','.join(breakdown) or 'total'}: {exc}"
                        errors.append(descriptor)
                        print(descriptor, file=sys.stderr)
                    except Exception as exc:
                        stats.errors += 1
                        descriptor = f"{game['name']} / {spec.metric}: {type(exc).__name__}: {exc}"
                        errors.append(descriptor)
                        print(descriptor, file=sys.stderr)

            if game_had_success:
                successful_game_rows.append(int(game["row"]))

        if not args.dry_run:
            update_game_last_fetch(sheet, successful_game_rows, iso(utc_now()))
        status = "SUCCESS" if stats.errors == 0 else "PARTIAL"
        note = "Dry run" if args.dry_run else "Hourly collection completed"
        if errors:
            note += f". First errors: {' | '.join(errors[:3])}"
        append_log(sheet, started_at, status, stats, note[:5000])
        print(json.dumps(stats.__dict__, indent=2))
        return 0 if successful_game_rows or args.dry_run else 1

    except Exception as exc:
        print(f"Collector failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            if "sheet" in locals():
                stats.errors += 1
                append_log(sheet, started_at, "FAILED", stats, f"{type(exc).__name__}: {exc}"[:5000])
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
