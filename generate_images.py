#!/usr/bin/python3

import asyncio
import csv
import datetime as dt
import html
import json
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp

from github_stats import (
    ApiDegradation,
    ApiWaitStat,
    CommitWindow,
    MonthWindow,
    MonthlyCommitRecord,
    Stats,
    monthly_commit_identity_patterns,
    rolling_month_windows,
)
from language_icons import render_language_icon


MONTHLY_COMMITS_CACHE = os.path.join("generated", "monthly-commits.json")
EXPERIMENTAL_METRICS_CACHE = os.path.join("generated", "experimental-metrics.json")
STAR_HISTORY_CACHE = os.path.join("generated", "star-history.csv")
STAR_HISTORY_FIELDNAMES = [
    "date",
    "total_stargazers",
    "repo_count",
    "captured_at",
]
EXPERIMENTAL_CARD_NAMES = [
    "contribution-pulse",
    "contribution-mix",
    "impact",
    "language-momentum",
    "collaboration",
    "maintainer-health",
    "personal-bests",
    "trading-card",
    "repo-portfolio",
    "commit-velocity",
    "star-history-30-days",
    "star-history-90-days",
    "star-history-365-days",
]
EXPERIMENTAL_LANGUAGE_ICON_BASELINE_OFFSET = 3
COMMIT_VELOCITY_WINDOW_SPECS = (
    ("trailing_30_days", "30 days", 30),
    ("trailing_180_days", "6 months", 180),
    ("trailing_365_days", "365 days", 365),
)
STAR_HISTORY_WINDOW_SPECS = (
    ("30_days", "30 days", 30, 1),
    ("90_days", "90 days", 90, 3),
    ("365_days", "365 days", 365, 7),
)


################################################################################
# Helper Functions
################################################################################


def generate_output_folder() -> None:
    """
    Create the output folder if it does not already exist
    """
    if not os.path.isdir("generated"):
        os.mkdir("generated")


def validate_generated_output(output_folder: str = "generated") -> None:
    """
    Fail if generated badges contain known fallback/corruption sentinels.
    """
    overview_path = os.path.join(output_folder, "overview.svg")
    languages_path = os.path.join(output_folder, "languages.svg")
    monthly_commits_path = os.path.join(output_folder, "monthly-commits.svg")
    experimental_paths = [
        os.path.join(output_folder, f"experimental-{name}.svg")
        for name in EXPERIMENTAL_CARD_NAMES
    ]

    with open(overview_path, "r") as f:
        overview = f.read()
    with open(languages_path, "r") as f:
        languages = f.read()
    with open(monthly_commits_path, "r") as f:
        monthly_commits = f.read()

    overview_is_empty_fallback = (
        "No Name's GitHub Statistics" in overview
        and "Stars</td><td>0</td>" in overview
        and "Forks</td><td>0</td>" in overview
        and "Repositories with contributions</td><td>0</td>" in overview
    )
    languages_are_empty = (
        'class="progress-item"' not in languages
        and '<span class="lang">' not in languages
    )
    monthly_commits_are_empty = (
        "Monthly Commits" not in monthly_commits
        or 'class="bar' not in monthly_commits
    )

    if overview_is_empty_fallback or languages_are_empty or monthly_commits_are_empty:
        raise RuntimeError(
            "Generated SVG validation failed; refusing to commit corrupt stats."
        )

    if env_truthy("GENERATE_EXPERIMENTAL", default=True):
        missing = [path for path in experimental_paths if not os.path.exists(path)]
        if missing:
            raise RuntimeError(
                "Generated experimental SVG validation failed; missing "
                + ", ".join(missing)
            )
        unresolved = []
        for path in experimental_paths:
            with open(path, "r") as f:
                content = f.read()
            if "{{" in content or "}}" in content or "<svg" not in content:
                unresolved.append(path)
        if unresolved:
            raise RuntimeError(
                "Generated experimental SVG validation failed; corrupt "
                + ", ".join(unresolved)
            )


def _format_degradation_items(items: List[ApiDegradation], limit: int = 20) -> str:
    if not items:
        return "- None\n"

    lines = []
    for item in items[:limit]:
        lines.append(
            f"- `{item.repo}` `{item.endpoint}`: HTTP {item.status} "
            f"({item.category}) - {item.message}"
        )
    remaining = len(items) - limit
    if remaining > 0:
        lines.append(f"- ...and {remaining} more")
    return "\n".join(lines) + "\n"


def _format_wait_items(items: List[ApiWaitStat], limit: int = 10) -> str:
    if not items:
        return "- None\n"

    lines = []
    for item in items[:limit]:
        lines.append(
            f"- `{item.repo}` `{item.endpoint}`: waited "
            f"{item.wait_seconds:.1f}s over {item.retry_count} retries "
            f"({item.elapsed_seconds:.1f}s elapsed, HTTP {item.status} "
            f"{item.category})"
        )
    remaining = len(items) - limit
    if remaining > 0:
        lines.append(f"- ...and {remaining} more")
    return "\n".join(lines) + "\n"


def render_action_summary(report: Dict[str, Any]) -> str:
    stats = report["stats"]
    api = report["api"]
    traffic_items = [
        ApiDegradation(**item) for item in api["traffic_degraded"]
    ]
    monthly_commit_items = [
        ApiDegradation(**item) for item in api.get("monthly_commits_degraded", [])
    ]
    slowest_items = [
        ApiWaitStat(**item) for item in api["slowest_requests"]
    ]
    monthly = report.get("monthly_commits", {})
    monthly_total = monthly.get("total", 0)
    monthly_current = monthly.get("current_month", {})
    experimental = report.get("experimental", {})
    experimental_limited = experimental.get("limited_data", [])
    star_history = report.get("star_history", {})

    return f"""# GitHub Stats Image Generation

## Generated Stats

- Name: {stats["name"]}
- Stargazers: {stats["stargazers"]:,}
- Forks: {stats["forks"]:,}
- All-time contributions: {stats["total_contributions"]:,}
- Current-year contributions: {stats["current_year_contributions"]:,}
- Merged pull requests: {stats["merged_pull_requests"]:,}
- Monthly commits shown: {monthly_total:,}
- Current month commits: {monthly_current.get("count", 0):,}
- Repositories: {stats["repos"]:,}
- Repository views: {stats["views"]:,}
- Star history samples: {star_history.get("samples", 0):,}
- Latest star snapshot: {star_history.get("latest_date", "none")} ({star_history.get("latest_total", 0):,})
- Star history cards: {star_history.get("card_count", 0):,}

## Experimental Stats

- Experimental metrics enabled: {experimental.get("enabled", False)}
- Experimental cards: {len(experimental.get("cards", [])):,}
- Private repositories redacted: {experimental.get("private_repositories", 0):,}
- Limited experimental data: {len(experimental_limited):,}

## API Degradations

- Traffic views degraded: {len(traffic_items)}
- Monthly commit scans degraded: {len(monthly_commit_items)}

## API Wait Summary

- REST requests: {api["rest_requests_total"]:,}
- REST retries: {api["rest_retries_total"]:,}
- Total retry wait: {api["rest_wait_seconds_total"]:.1f}s
- Traffic views retry wait: {api["traffic_wait_seconds_total"]:.1f}s
- Monthly commit scan retry wait: {api.get("monthly_commits_wait_seconds_total", 0.0):.1f}s

### Slowest API Waits

{_format_wait_items(slowest_items)}
### Traffic View Failures

{_format_degradation_items(traffic_items)}
### Monthly Commit Scan Warnings

{_format_degradation_items(monthly_commit_items)}
"""


def validate_run_report(report: Dict[str, Any]) -> None:
    """
    Fail when strict validation requires unavailable optional metrics.
    """
    stats = report["stats"]
    api = report["api"]
    repo_count = stats["repos"]
    failures = []

    traffic_degraded_count = len(api["traffic_degraded"])

    strict_traffic_views = env_truthy("STRICT_TRAFFIC_VIEW_VALIDATION")
    if (
        strict_traffic_views
        and repo_count > 0
        and stats["views"] == 0
        and traffic_degraded_count > 0
    ):
        failures.append(
            "Repository views is zero while "
            f"{traffic_degraded_count} traffic endpoints degraded."
        )

    if failures:
        raise RuntimeError(
            "Generated metrics failed validation:\n- " + "\n- ".join(failures)
        )


async def build_run_report(s: Stats) -> Dict[str, Any]:
    monthly_cache = load_monthly_commits_cache()
    experimental_cache = load_monthly_commits_cache(EXPERIMENTAL_METRICS_CACHE)
    star_history_records = load_star_history_cache()
    months = monthly_cache.get("months", [])
    current_month = next(
        (item for item in months if item.get("is_current")),
        {},
    )
    latest_star_history = star_history_records[-1] if star_history_records else {}
    star_history_cards = len(
        experimental_cache.get("star_history", {}).get("windows", [])
    )
    return {
        "stats": {
            "name": await s.name,
            "stargazers": await s.stargazers,
            "forks": await s.forks,
            "total_contributions": await s.total_contributions,
            "current_year_contributions": await s.current_year_contributions,
            "merged_pull_requests": await s.merged_pull_requests,
            "repos": len(await s.repos),
            "views": await s.views,
        },
        "monthly_commits": {
            "total": sum(int(item.get("count", 0)) for item in months),
            "current_month": current_month,
            "month_count": len(months),
        },
        "star_history": {
            "samples": len(star_history_records),
            "latest_date": latest_star_history.get("date", ""),
            "latest_total": int(latest_star_history.get("total_stargazers", 0))
            if latest_star_history
            else 0,
            "card_count": star_history_cards,
        },
        "experimental": {
            "enabled": env_truthy("GENERATE_EXPERIMENTAL", default=True),
            "cards": [
                f"experimental-{name}.svg" for name in EXPERIMENTAL_CARD_NAMES
            ]
            if experimental_cache
            else [],
            "private_repositories": experimental_cache.get("privacy", {}).get(
                "private_repositories",
                0,
            ),
            "limited_data": experimental_cache.get("limited_data", []),
        },
        "api": s.report.to_dict(),
        "github": {
            "run_id": os.getenv("GITHUB_RUN_ID", ""),
            "sha": os.getenv("GITHUB_SHA", ""),
            "ref": os.getenv("GITHUB_REF", ""),
            "actor": os.getenv("GITHUB_ACTOR", ""),
        },
    }


def write_run_report(
    report: Dict[str, Any],
    output_path: str = "run-report.json",
) -> None:
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")


def write_action_summary(summary: str) -> None:
    print(summary)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a") as f:
        f.write(summary)
        f.write("\n")


def env_truthy(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() not in {"", "0", "false", "no", "off"}


def _strip_env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env_file(path: str = ".env") -> None:
    """
    Load simple KEY=value pairs from a local env file.

    Existing process environment values win so CI secrets and shell overrides
    remain authoritative.
    """
    if not os.path.exists(path):
        return

    with open(path, "r") as f:
        for line_number, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise RuntimeError(
                    f"{path}:{line_number} is not a KEY=value assignment."
                )
            key, raw_value = line.split("=", 1)
            key = key.strip()
            if not key:
                raise RuntimeError(f"{path}:{line_number} has an empty key.")
            if key in os.environ:
                continue
            os.environ[key] = _strip_env_value(raw_value)


def parse_repo_list(raw_value: Optional[str]) -> Set[str]:
    if not raw_value:
        return set()
    return {item.strip() for item in raw_value.split(",") if item.strip()}


def load_monthly_commits_cache(
    path: str = MONTHLY_COMMITS_CACHE,
) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def write_monthly_commits_cache(
    data: Dict[str, Any],
    path: str = MONTHLY_COMMITS_CACHE,
) -> None:
    generate_output_folder()
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def write_experimental_metrics_cache(
    data: Dict[str, Any],
    path: str = EXPERIMENTAL_METRICS_CACHE,
) -> None:
    generate_output_folder()
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _parse_star_history_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    date_value = str(row.get("date", "")).strip()
    if not date_value:
        return None
    try:
        dt.date.fromisoformat(date_value)
        total_stargazers = int(row.get("total_stargazers", ""))
        repo_count = int(row.get("repo_count", 0))
    except (TypeError, ValueError):
        return None

    captured_at = str(row.get("captured_at", "")).strip()
    if not captured_at:
        captured_at = f"{date_value}T00:00:00Z"
    return {
        "date": date_value,
        "total_stargazers": total_stargazers,
        "repo_count": repo_count,
        "captured_at": captured_at,
    }


def load_star_history_cache(
    path: str = STAR_HISTORY_CACHE,
) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        records = [
            parsed
            for row in reader
            if (parsed := _parse_star_history_row(row)) is not None
        ]
    return sorted(records, key=lambda item: item["date"])


def write_star_history_cache(
    records: List[Dict[str, Any]],
    path: str = STAR_HISTORY_CACHE,
) -> None:
    generate_output_folder()
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    sorted_records = sorted(records, key=lambda item: item["date"])
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=STAR_HISTORY_FIELDNAMES)
        writer.writeheader()
        for record in sorted_records:
            writer.writerow(
                {
                    "date": record["date"],
                    "total_stargazers": int(record["total_stargazers"]),
                    "repo_count": int(record.get("repo_count", 0)),
                    "captured_at": record["captured_at"],
                }
            )


def record_star_history_sample(
    total_stargazers: int,
    repo_count: int,
    now: Optional[dt.datetime] = None,
    path: str = STAR_HISTORY_CACHE,
) -> Dict[str, Any]:
    current_time = _as_utc(now or dt.datetime.now(dt.timezone.utc))
    record = {
        "date": current_time.date().isoformat(),
        "total_stargazers": int(total_stargazers),
        "repo_count": int(repo_count),
        "captured_at": _format_utc_datetime(current_time),
    }
    records_by_date = {
        item["date"]: item for item in load_star_history_cache(path)
    }
    records_by_date[record["date"]] = record
    records = sorted(records_by_date.values(), key=lambda item: item["date"])
    write_star_history_cache(records, path)
    return {
        "latest": record,
        "samples": len(records),
    }


def _monthly_cache_records(cache: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    records = cache.get("months", [])
    if not isinstance(records, list):
        return {}
    return {
        item["key"]: item
        for item in records
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    }


def _record_from_window(window: MonthWindow, count: int) -> MonthlyCommitRecord:
    return MonthlyCommitRecord(
        key=window.key,
        label=window.label,
        count=count,
        is_current=window.is_current,
    )


async def build_monthly_commit_cache(
    s: Stats,
    force_backfill: bool = False,
    now: Optional[dt.datetime] = None,
    cache_path: str = MONTHLY_COMMITS_CACHE,
) -> Dict[str, Any]:
    windows = rolling_month_windows(now)
    existing = {} if force_backfill else load_monthly_commits_cache(cache_path)
    existing_by_key = _monthly_cache_records(existing)
    windows_to_scan = [
        window
        for window in windows
        if force_backfill or window.is_current or window.key not in existing_by_key
    ]

    patterns = monthly_commit_identity_patterns(s.username)
    scanned_counts: Dict[str, int] = {}
    audit_counts: Dict[str, int] = {}
    degraded_scan_keys: Set[str] = set()
    repo_count = len(await s.repos)
    if windows_to_scan:
        audit_counts = await s.audit_monthly_commit_counts(windows_to_scan)
        scan_result = await s.scan_monthly_commits(
            windows_to_scan,
            identity_patterns=patterns,
            extra_repos=parse_repo_list(os.getenv("MONTHLY_COMMITS_EXTRA_REPOS")),
        )
        scanned_counts = scan_result.counts
        degraded_scan_keys = scan_result.degraded_months
        repo_count = scan_result.repo_count

    records = []
    for window in windows:
        should_preserve_cached_count = (
            window.key in degraded_scan_keys and window.key in existing_by_key
        )
        if window.key in scanned_counts and not should_preserve_cached_count:
            record = _record_from_window(window, scanned_counts[window.key])
        else:
            cached_count = int(existing_by_key.get(window.key, {}).get("count", 0))
            record = _record_from_window(window, cached_count)
        record_data = record.__dict__
        record_data["scan_degraded"] = window.key in degraded_scan_keys
        if window.key in audit_counts:
            record_data["github_attributed_count"] = audit_counts[window.key]
        else:
            record_data["github_attributed_count"] = int(
                existing_by_key.get(window.key, {}).get(
                    "github_attributed_count",
                    0,
                )
            )
        records.append(record_data)

    cache = {
        "generated_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "window": {
            "months": len(windows),
            "timezone": "UTC",
            "mode": "current_plus_12",
        },
        "source": {
            "counting": "rest_commits_identity_scan",
            "audit_counting": "graphql_total_commit_contributions",
            "identity_patterns": patterns,
            "repo_count": repo_count,
            "scanned_months": len(windows_to_scan),
            "degraded_months": len(degraded_scan_keys),
            "degraded_repo_months": len(s.report.monthly_commits_degraded),
        },
        "months": records,
    }
    write_monthly_commits_cache(cache, cache_path)
    return cache


def _parse_iso_datetime(value: str) -> Optional[dt.datetime]:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _repo_age_days(value: str, now: dt.datetime) -> Optional[int]:
    parsed = _parse_iso_datetime(value)
    if parsed is None:
        return None
    return max(0, (now - parsed).days)


def _active_month_streak(months: List[Dict[str, Any]]) -> int:
    streak = 0
    for item in reversed(months):
        if int(item.get("count", 0)) <= 0:
            break
        streak += 1
    return streak


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _format_utc_datetime(value: dt.datetime) -> str:
    return (
        _as_utc(value)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def commit_velocity_windows(now: Optional[dt.datetime] = None) -> List[CommitWindow]:
    current_time = _as_utc(now or dt.datetime.now(dt.timezone.utc))
    return [
        CommitWindow(
            key=key,
            label=label,
            since=_format_utc_datetime(current_time - dt.timedelta(days=days)),
            until=_format_utc_datetime(current_time),
        )
        for key, label, days in COMMIT_VELOCITY_WINDOW_SPECS
    ]


async def build_commit_velocity_metrics(
    s: Stats,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    windows = commit_velocity_windows(now)
    scan_result = await s.scan_commit_windows(
        windows,
        identity_patterns=monthly_commit_identity_patterns(s.username),
        extra_repos=parse_repo_list(os.getenv("MONTHLY_COMMITS_EXTRA_REPOS")),
    )

    window_days = {key: days for key, _, days in COMMIT_VELOCITY_WINDOW_SPECS}
    velocity_windows = []
    for window in windows:
        days = window_days[window.key]
        commits = int(scan_result.counts.get(window.key, 0))
        hours = days * 24
        weeks = days / 7
        months = days / 30
        velocity_windows.append(
            {
                "key": window.key,
                "label": window.label,
                "days": days,
                "commits": commits,
                "per_hour": round(commits / hours, 4),
                "per_day": round(commits / days, 2),
                "per_week": round(commits / weeks, 2),
                "per_month": round(commits / months, 2),
                "scan_degraded": window.key in scan_result.degraded_windows,
            }
        )

    return {
        "windows": velocity_windows,
        "source": {
            "counting": "rest_commits_identity_scan",
            "timezone": "UTC",
            "repo_count": scan_result.repo_count,
            "degraded_windows": len(scan_result.degraded_windows),
        },
    }


def _parse_star_history_date(value: str) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _star_history_window_points(
    records: List[Dict[str, Any]],
    now: dt.datetime,
    window_days: int,
    bucket_days: int,
) -> Tuple[List[Dict[str, Any]], int]:
    current_date = _as_utc(now).date()
    start_date = current_date - dt.timedelta(days=window_days - 1)
    filtered = []
    for record in records:
        maybe_date = _parse_star_history_date(str(record.get("date", "")))
        if maybe_date is None or maybe_date < start_date or maybe_date > current_date:
            continue
        filtered.append((maybe_date, record))

    latest_by_bucket: Dict[int, Tuple[dt.date, Dict[str, Any]]] = {}
    for record_date, record in filtered:
        bucket_index = (record_date - start_date).days // bucket_days
        existing = latest_by_bucket.get(bucket_index)
        if existing is None or record_date >= existing[0]:
            latest_by_bucket[bucket_index] = (record_date, record)

    points = []
    for record_date, record in sorted(latest_by_bucket.values(), key=lambda item: item[0]):
        points.append(
            {
                "date": record_date.isoformat(),
                "offset_days": (record_date - start_date).days,
                "total_stargazers": int(record.get("total_stargazers", 0)),
                "repo_count": int(record.get("repo_count", 0)),
            }
        )
    return points, len(filtered)


def build_star_history_metrics(
    now: Optional[dt.datetime] = None,
    cache_path: str = STAR_HISTORY_CACHE,
) -> Dict[str, Any]:
    current_time = _as_utc(now or dt.datetime.now(dt.timezone.utc))
    records = load_star_history_cache(cache_path)
    windows = []
    for key, label, days, bucket_days in STAR_HISTORY_WINDOW_SPECS:
        points, raw_sample_count = _star_history_window_points(
            records,
            current_time,
            days,
            bucket_days,
        )
        if points:
            latest_total = int(points[-1]["total_stargazers"])
            delta = latest_total - int(points[0]["total_stargazers"])
            latest_date = points[-1]["date"]
        else:
            latest_total = 0
            delta = 0
            latest_date = ""
        windows.append(
            {
                "key": key,
                "label": label,
                "days": days,
                "bucket_days": bucket_days,
                "latest_total": latest_total,
                "delta": delta,
                "latest_date": latest_date,
                "sample_count": len(points),
                "raw_sample_count": raw_sample_count,
                "points": points,
            }
        )

    latest_record = records[-1] if records else {}
    return {
        "source": {
            "counting": "daily_aggregate_stargazer_snapshot",
            "path": cache_path,
            "timezone": "UTC",
            "samples": len(records),
            "latest_date": latest_record.get("date", ""),
            "latest_total": int(latest_record.get("total_stargazers", 0))
            if latest_record
            else 0,
        },
        "windows": windows,
    }


def _bounded_score(value: float, maximum: float) -> int:
    if maximum <= 0:
        return 0
    return max(0, min(100, round((value / maximum) * 100)))


async def _optional_metric(awaitable: Any, label: str) -> Any:
    try:
        return await awaitable
    except Exception as error:
        return {"limited": label, "error": str(error)}


def _repo_score(repo: Dict[str, Any]) -> int:
    return (
        int(repo.get("stargazers", 0))
        + (2 * int(repo.get("forks", 0)))
        + int(repo.get("open_issues", 0))
        + int(repo.get("open_pull_requests", 0))
    )


def _top_repositories(repos: List[Dict[str, Any]], limit: int = 6) -> List[Dict[str, Any]]:
    ranked = sorted(
        repos,
        key=lambda repo: (_repo_score(repo), repo.get("display_name", "")),
        reverse=True,
    )
    return [
        {
            "display_name": repo.get("display_name", "Unknown"),
            "is_private": bool(repo.get("is_private", False)),
            "score": _repo_score(repo),
            "stars": int(repo.get("stargazers", 0)),
            "forks": int(repo.get("forks", 0)),
        }
        for repo in ranked[:limit]
    ]


async def build_experimental_metrics(
    s: Stats,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """
    Build redacted, approximate data for experimental SVG cards.
    """
    current_time = now or dt.datetime.now(dt.timezone.utc)
    monthly_cache = load_monthly_commits_cache()
    months = [
        item for item in monthly_cache.get("months", []) if isinstance(item, dict)
    ]
    monthly_total = sum(int(item.get("count", 0)) for item in months)
    active_months = sum(1 for item in months if int(item.get("count", 0)) > 0)
    best_month = max(months, key=lambda item: int(item.get("count", 0)), default={})
    current_month = next((item for item in months if item.get("is_current")), {})
    average_monthly = round(monthly_total / len(months), 1) if months else 0.0
    active_streak = _active_month_streak(months)

    repo_values = [repo.to_safe_dict() for repo in (await s.repo_metrics).values()]
    private_repo_count = sum(1 for repo in repo_values if repo["is_private"])
    public_repo_count = len(repo_values) - private_repo_count
    archived_repo_count = sum(1 for repo in repo_values if repo["is_archived"])
    recently_updated_count = sum(
        1
        for repo in repo_values
        if (maybe_age := _repo_age_days(repo.get("pushed_at", ""), current_time))
        is not None
        and maybe_age <= 90
    )
    stale_repo_count = sum(
        1
        for repo in repo_values
        if (maybe_age := _repo_age_days(repo.get("pushed_at", ""), current_time))
        is not None
        and maybe_age > 365
    )
    release_repo_count = sum(
        1 for repo in repo_values if int(repo.get("releases", 0)) > 0
    )
    tag_repo_count = sum(1 for repo in repo_values if int(repo.get("tags", 0)) > 0)
    owned_repo_count = sum(1 for repo in repo_values if not repo["is_fork"])
    external_repo_count = len(repo_values) - owned_repo_count

    breakdown = await _optional_metric(
        s.current_year_contribution_breakdown,
        "current-year contribution breakdown unavailable",
    )
    if isinstance(breakdown, dict):
        contribution_breakdown = {
            "commits": 0,
            "issues": 0,
            "pull_requests": 0,
            "pull_request_reviews": 0,
            "repositories": 0,
            "restricted": 0,
            "total": 0,
        }
        limited_data = [breakdown["limited"]]
    else:
        contribution_breakdown = breakdown.to_dict()
        limited_data = []

    views = await _optional_metric(s.views, "repository traffic views unavailable")
    if isinstance(views, dict):
        views_total = 0
        limited_data.append(views["limited"])
    else:
        views_total = int(views)

    clones = await _optional_metric(s.clones, "repository traffic clones unavailable")
    if isinstance(clones, dict):
        clones_total = 0
        limited_data.append(clones["limited"])
    else:
        clones_total = int(clones)
    if s.report.traffic_degraded:
        limited_data.append("repository traffic data degraded")
    if s.report.monthly_commits_degraded:
        limited_data.append("monthly commit scan data degraded")
    commit_velocity = await build_commit_velocity_metrics(s, now=current_time)
    if int(commit_velocity["source"].get("degraded_windows", 0)) > 0:
        limited_data.append("commit velocity scan data degraded")
    star_history = build_star_history_metrics(now=current_time)

    languages: Dict[str, Dict[str, Any]] = {}
    for repo in repo_values:
        maybe_age = _repo_age_days(repo.get("pushed_at", ""), current_time)
        if maybe_age is None:
            recency_weight = 1.0
        elif maybe_age <= 30:
            recency_weight = 4.0
        elif maybe_age <= 90:
            recency_weight = 2.5
        elif maybe_age <= 365:
            recency_weight = 1.25
        else:
            recency_weight = 0.5
        for lang, data in repo.get("languages", {}).items():
            weighted_size = int(data.get("size", 0)) * recency_weight
            if lang not in languages:
                languages[lang] = {
                    "score": 0.0,
                    "color": data.get("color"),
                    "repos": 0,
                }
            languages[lang]["score"] += weighted_size
            languages[lang]["repos"] += 1
    language_total = sum(item["score"] for item in languages.values())
    language_momentum = [
        {
            "language": lang,
            "percent": round(100 * data["score"] / language_total, 2)
            if language_total
            else 0.0,
            "color": data.get("color") or "#8b949e",
            "repos": data["repos"],
        }
        for lang, data in sorted(
            languages.items(),
            key=lambda item: item[1]["score"],
            reverse=True,
        )[:8]
    ]

    top_repositories = _top_repositories(repo_values)
    top_public_repo = next(
        (repo for repo in top_repositories if not repo["is_private"]),
        {"display_name": "No public repo", "score": 0, "stars": 0, "forks": 0},
    )
    issue_total = sum(int(repo.get("open_issues", 0)) for repo in repo_values)
    pr_total = sum(int(repo.get("open_pull_requests", 0)) for repo in repo_values)

    activity_score = _bounded_score(
        monthly_total + contribution_breakdown["total"], 1000
    )
    impact_score = _bounded_score(
        (await s.stargazers) + (2 * await s.forks) + views_total + clones_total,
        2000,
    )
    collaboration_score = _bounded_score(
        contribution_breakdown["pull_requests"]
        + contribution_breakdown["pull_request_reviews"]
        + contribution_breakdown["issues"],
        500,
    )
    breadth_score = _bounded_score(len(repo_values) + active_months, 200)

    metrics = {
        "generated_at": current_time.replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "limited_data": sorted(set(limited_data)),
        "privacy": {
            "private_repository_names": "redacted",
            "private_repositories": private_repo_count,
        },
        "contribution_pulse": {
            "total": monthly_total,
            "active_months": active_months,
            "month_count": len(months),
            "average_monthly": average_monthly,
            "best_month": best_month.get("label", "None"),
            "best_month_count": int(best_month.get("count", 0)),
            "current_month": current_month.get("label", "None"),
            "current_month_count": int(current_month.get("count", 0)),
            "active_streak_months": active_streak,
        },
        "contribution_mix": contribution_breakdown,
        "impact": {
            "stars": await s.stargazers,
            "forks": await s.forks,
            "views_14_days": views_total,
            "clones_14_days": clones_total,
            "top_public_repo": top_public_repo,
        },
        "language_momentum": language_momentum,
        "collaboration": {
            "merged_pull_requests": await s.merged_pull_requests,
            "pull_request_reviews": contribution_breakdown[
                "pull_request_reviews"
            ],
            "issues": contribution_breakdown["issues"],
            "owned_repositories": owned_repo_count,
            "external_repositories": external_repo_count,
        },
        "maintainer_health": {
            "open_issues": issue_total,
            "open_pull_requests": pr_total,
            "recently_updated": recently_updated_count,
            "stale_repositories": stale_repo_count,
            "archived_repositories": archived_repo_count,
            "release_repositories": release_repo_count,
            "tagged_repositories": tag_repo_count,
        },
        "personal_bests": {
            "best_month": best_month.get("label", "None"),
            "best_month_count": int(best_month.get("count", 0)),
            "active_streak_months": active_streak,
            "top_public_repo": top_public_repo["display_name"],
            "top_public_repo_score": top_public_repo["score"],
        },
        "trading_card": {
            "activity_score": activity_score,
            "impact_score": impact_score,
            "collaboration_score": collaboration_score,
            "breadth_score": breadth_score,
            "primary_language": language_momentum[0]["language"]
            if language_momentum
            else "Unknown",
        },
        "repo_portfolio": {
            "repositories": top_repositories,
            "public_repositories": public_repo_count,
            "private_repositories": private_repo_count,
        },
        "commit_velocity": commit_velocity,
        "star_history": star_history,
        "repositories": repo_values,
    }
    write_experimental_metrics_cache(metrics)
    return metrics


################################################################################
# Individual Image Generation Functions
################################################################################


async def generate_overview(s: Stats) -> None:
    """
    Generate an SVG badge with summary statistics
    :param s: Represents user's GitHub statistics
    """
    with open("templates/overview.svg", "r") as f:
        output = f.read()

    output = re.sub("{{ name }}", await s.name, output)
    output = re.sub("{{ stars }}", f"{await s.stargazers:,}", output)
    output = re.sub("{{ forks }}", f"{await s.forks:,}", output)
    output = re.sub("{{ contributions }}", f"{await s.total_contributions:,}", output)
    output = re.sub(
        "{{ merged_pull_requests }}", f"{await s.merged_pull_requests:,}", output
    )
    output = re.sub("{{ views }}", f"{await s.views:,}", output)
    output = re.sub("{{ repos }}", f"{len(await s.repos):,}", output)

    generate_output_folder()
    with open("generated/overview.svg", "w") as f:
        f.write(output)


async def generate_languages(s: Stats) -> None:
    """
    Generate an SVG badge with summary languages used
    :param s: Represents user's GitHub statistics
    """
    with open("templates/languages.svg", "r") as f:
        output = f.read()

    progress = ""
    lang_list = ""
    sorted_languages = sorted(
        (await s.languages).items(), reverse=True, key=lambda t: t[1].get("size")
    )
    delay_between = 150
    for i, (lang, data) in enumerate(sorted_languages):
        color = data.get("color")
        color = color if color is not None else "#000000"
        progress += (
            f'<span style="background-color: {color};'
            f'width: {data.get("prop", 0):0.3f}%;" '
            f'class="progress-item"></span>'
        )
        lang_list += f"""
<li style="animation-delay: {i * delay_between}ms;">
{render_language_icon(lang, color)}
<span class="lang">{_svg_text(lang)}</span>
<span class="percent">{data.get("prop", 0):0.2f}%</span>
</li>

"""

    output = re.sub(r"{{ progress }}", progress, output)
    output = re.sub(r"{{ lang_list }}", lang_list, output)

    generate_output_folder()
    with open("generated/languages.svg", "w") as f:
        f.write(output)


def _monthly_commit_bars(months: List[Dict[str, Any]]) -> str:
    max_count = max([int(item.get("count", 0)) for item in months] + [1])
    chart_bottom = 153
    chart_height = 82
    left = 27
    step = 24
    bar_width = 14
    bars = []

    for index, item in enumerate(months):
        count = int(item.get("count", 0))
        height = 1 if count == 0 else max(3, round((count / max_count) * chart_height))
        x = left + index * step
        y = chart_bottom - height
        label = html.escape(str(item.get("label", "")))
        class_name = "bar current" if item.get("is_current") else "bar"
        bars.append(
            f'<g style="animation-delay: {index * 75}ms">'
            f"<title>{label}: {count:,} commits</title>"
            f'<rect class="{class_name}" x="{x}" y="{y}" '
            f'width="{bar_width}" height="{height}" rx="2" />'
            f'<text class="count" x="{x + bar_width / 2:.1f}" y="{y - 5}" '
            f'text-anchor="middle">{count:,}</text>'
            f'<text class="month" x="{x + bar_width / 2:.1f}" y="174" '
            f'text-anchor="end" transform="rotate(-35 {x + bar_width / 2:.1f} 174)">'
            f"{label}</text>"
            "</g>"
        )

    return "\n".join(bars)


async def generate_monthly_commits(
    s: Stats,
    force_backfill: bool = False,
    now: Optional[dt.datetime] = None,
    cache_path: str = MONTHLY_COMMITS_CACHE,
) -> None:
    cache = await build_monthly_commit_cache(
        s,
        force_backfill=force_backfill,
        now=now,
        cache_path=cache_path,
    )

    with open("templates/monthly-commits.svg", "r") as f:
        output = f.read()

    months = cache.get("months", [])
    current_month = next(
        (item for item in months if isinstance(item, dict) and item.get("is_current")),
        {},
    )
    total = sum(int(item.get("count", 0)) for item in months if isinstance(item, dict))
    output = re.sub(r"{{ bars }}", _monthly_commit_bars(months), output)
    output = re.sub(r"{{ total }}", f"{total:,}", output)
    output = re.sub(
        r"{{ current_month }}",
        html.escape(str(current_month.get("label", ""))),
        output,
    )
    output = re.sub(
        r"{{ current_count }}",
        f"{int(current_month.get('count', 0)):,}",
        output,
    )

    generate_output_folder()
    with open("generated/monthly-commits.svg", "w") as f:
        f.write(output)


def _svg_text(value: Any) -> str:
    return html.escape(str(value))


def _format_number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _shorten_text(value: Any, max_chars: Optional[int]) -> Tuple[str, bool]:
    text = str(value)
    if max_chars is None or max_chars <= 0 or len(text) <= max_chars:
        return text, False
    if max_chars <= 3:
        return text[:max_chars], True
    return f"{text[:max_chars - 3]}...", True


def _experimental_icon_y_for_text_baseline(text_y: int, icon_size: int) -> int:
    return text_y - icon_size + EXPERIMENTAL_LANGUAGE_ICON_BASELINE_OFFSET


def _experimental_rows(
    rows: List[Tuple[str, Any]],
    start_y: int = 76,
    row_gap: int = 18,
    value_max_chars: Optional[int] = None,
    value_icon_labels: Optional[Set[str]] = None,
    value_icon_x: int = 248,
    value_icon_size: int = 14,
) -> str:
    output = []
    for index, (label, value) in enumerate(rows[:7]):
        y = start_y + (index * row_gap)
        display_value, value_shortened = _shorten_text(value, value_max_chars)
        title = (
            f"<title>{_svg_text(label)}: {_svg_text(value)}</title>"
            if value_shortened
            else ""
        )
        value_icon = ""
        if value_icon_labels is not None and label in value_icon_labels:
            value_icon = render_language_icon(
                str(value),
                "#8b949e",
                class_name="language-icon experimental-language-icon",
                size=value_icon_size,
                x=value_icon_x,
                y=_experimental_icon_y_for_text_baseline(y, value_icon_size),
            )
        output.append(
            f'<g class="row" style="animation-delay: {index * 80}ms">'
            f"{title}"
            f'<text class="label" x="21" y="{y}">{_svg_text(label)}</text>'
            f"{value_icon}"
            f'<text class="value" x="330" y="{y}" text-anchor="end">'
            f"{_svg_text(display_value)}</text></g>"
        )
    return "\n".join(output)


def _experimental_horizontal_bars(
    items: List[Dict[str, Any]],
    label_key: str,
    value_key: str,
    start_y: int = 74,
    color_key: Optional[str] = None,
    label_max_chars: Optional[int] = None,
    value_max_chars: Optional[int] = None,
    bar_x: int = 182,
    bar_max_width: int = 130,
    value_x: int = 330,
    value_position: str = "after_bar",
    label_icon_key: Optional[str] = None,
    label_icon_color_key: Optional[str] = None,
    label_icon_size: int = 14,
) -> str:
    max_value = max([float(item.get(value_key, 0)) for item in items] + [1.0])
    output = []
    for index, item in enumerate(items[:6]):
        y = start_y + (index * 20)
        value = float(item.get(value_key, 0))
        width = max(2, round((value / max_value) * bar_max_width))
        color = item.get(color_key or "color", "#2da44e")
        label = item.get(label_key, "Unknown")
        display_label, label_shortened = _shorten_text(label, label_max_chars)
        raw_value = item.get("display_value", item.get(value_key, 0))
        display_value, value_shortened = _shorten_text(raw_value, value_max_chars)
        title = (
            f"<title>{_svg_text(label)}: {_svg_text(raw_value)}</title>"
            if label_shortened or value_shortened
            else ""
        )
        label_icon = ""
        label_x = 21
        if label_icon_key is not None:
            label_icon = render_language_icon(
                str(item.get(label_icon_key, label)),
                str(item.get(label_icon_color_key or color_key or "color", color)),
                class_name="language-icon experimental-language-icon",
                size=label_icon_size,
                x=21,
                y=_experimental_icon_y_for_text_baseline(y, label_icon_size),
            )
            label_x = 41
        value_text = (
            f'<text class="value" x="{value_x}" y="{y}" text-anchor="end">'
            f"{_svg_text(display_value)}</text>"
        )
        bar = (
            f'<rect class="metric-bar" x="{bar_x}" y="{y - 10}" width="{width}" '
            f'height="10" rx="2" style="fill:{_svg_text(color)}" />'
        )
        value_and_bar = (
            value_text + bar if value_position == "before_bar" else bar + value_text
        )
        output.append(
            f'<g class="row" style="animation-delay: {index * 80}ms">'
            f"{title}"
            f"{label_icon}"
            f'<text class="label" x="{label_x}" y="{y}">'
            f'{_svg_text(display_label)}</text>'
            f"{value_and_bar}</g>"
        )
    return "\n".join(output)


def _experimental_stack_bar(
    values: List[Tuple[str, int, str]],
    x: int = 21,
    y: int = 70,
    width: int = 318,
    height: int = 8,
) -> str:
    total = sum(value for _, value, _ in values if value > 0)
    clip_id = "contribution-mix-stack-clip"
    if total <= 0:
        return (
            f'<rect class="stack-track" x="{x}" y="{y}" '
            f'width="{width}" height="{height}" rx="6" />'
        )

    current_x = x
    output = [
        f"<defs><clipPath id=\"{clip_id}\">"
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="6" />'
        f"</clipPath></defs>",
        f'<rect class="stack-track" x="{x}" y="{y}" '
        f'width="{width}" height="{height}" rx="6" />',
        f'<g clip-path="url(#{clip_id})">',
    ]
    positive_values = [
        (label, value, color) for label, value, color in values if value > 0
    ]
    segment_widths = [
        max(1, int((value / total) * width))
        for _, value, _ in positive_values
    ]
    remaining_width = width - sum(segment_widths)
    remainders = sorted(
        enumerate(
            ((value / total) * width) - int((value / total) * width)
            for _, value, _ in positive_values
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    for index, _ in remainders[:remaining_width]:
        segment_widths[index] += 1

    for (label, value, color), segment_width in zip(positive_values, segment_widths):
        if segment_width <= 0:
            continue
        output.append(
            f"<title>{_svg_text(label)}: {_format_number(value)}</title>"
            f'<rect class="metric-bar stack-segment" x="{current_x}" y="{y}" '
            f'width="{segment_width}" height="{height}" '
            f'style="fill:{_svg_text(color)}" />'
        )
        current_x += segment_width
    output.append("</g>")
    return "\n".join(output)


def _format_velocity_rate(value: Any, precision: int) -> str:
    try:
        return f"{float(value):.{precision}f}"
    except (TypeError, ValueError):
        return str(value)


def _commit_velocity_table(windows: List[Dict[str, Any]]) -> str:
    header_y = 86
    divider_y = 96
    first_row_y = 118
    row_gap = 30
    columns = [
        ("label", "Window", 21, "label", "start"),
        ("per_hour", "/ hr", 140, "value", "middle"),
        ("per_day", "/ day", 200, "value", "middle"),
        ("per_week", "/ wk", 258, "value", "middle"),
        ("per_month", "/ mo", 316, "value", "middle"),
    ]
    output = [
        '<g class="row" style="animation-delay: 0ms">'
        + "".join(
            f'<text class="label" x="{x}" y="{header_y}" text-anchor="{anchor}">'
            f"{_svg_text(label)}</text>"
            for _, label, x, _, anchor in columns
        )
        + "</g>",
        f'<line class="table-divider" x1="21" y1="{divider_y}" x2="339" y2="{divider_y}" />',
    ]

    for index, item in enumerate(windows[:3]):
        y = first_row_y + (index * row_gap)
        label = item.get("label", "Unknown")
        commits = _format_number(item.get("commits", 0))
        per_hour = _format_velocity_rate(item.get("per_hour", 0), 2)
        per_day = _format_velocity_rate(item.get("per_day", 0), 1)
        per_week = _format_velocity_rate(item.get("per_week", 0), 1)
        per_month = _format_velocity_rate(item.get("per_month", 0), 1)
        row_values = {
            "label": label,
            "per_hour": per_hour,
            "per_day": per_day,
            "per_week": per_week,
            "per_month": per_month,
        }
        row_cells = "".join(
            f'<text class="{class_name}" x="{x}" y="{y}" text-anchor="{anchor}">'
            f"{_svg_text(row_values[key])}</text>"
            for key, _, x, class_name, anchor in columns
        )
        output.append(
            f'<g class="row" style="animation-delay: {(index + 1) * 80}ms">'
            f"<title>{_svg_text(label)}: {commits} commits; "
            f"{per_hour}/hr; {per_day}/day; {per_week}/wk; "
            f"{per_month}/mo</title>"
            f"{row_cells}"
            "</g>"
        )
    return "\n".join(output)


def _render_experimental_template(
    name: str,
    title: str,
    subtitle: str,
    body: str,
) -> None:
    with open(f"templates/experimental-{name}.svg", "r") as f:
        output = f.read()
    output = re.sub(r"{{ title }}", _svg_text(title), output)
    output = re.sub(r"{{ subtitle }}", _svg_text(subtitle), output)
    output = re.sub(r"{{ body }}", body, output)
    generate_output_folder()
    with open(f"generated/experimental-{name}.svg", "w") as f:
        f.write(output)


def _render_star_history_template(
    name: str,
    title: str,
    subtitle: str,
    body: str,
) -> None:
    with open("templates/experimental-star-history.svg", "r") as f:
        output = f.read()
    output = re.sub(r"{{ title }}", _svg_text(title), output)
    output = re.sub(r"{{ subtitle }}", _svg_text(subtitle), output)
    output = re.sub(r"{{ body }}", body, output)
    generate_output_folder()
    with open(f"generated/experimental-{name}.svg", "w") as f:
        f.write(output)


def _limited_suffix(metrics: Dict[str, Any]) -> str:
    return "limited data" if metrics.get("limited_data") else "experimental"


def _format_signed_number(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    if number > 0:
        return f"+{number:,}"
    return f"{number:,}"


def _star_history_chart(window: Dict[str, Any]) -> str:
    points = [
        point for point in window.get("points", []) if isinstance(point, dict)
    ]
    chart_x = 24
    chart_y = 78
    chart_width = 312
    chart_height = 78
    chart_bottom = chart_y + chart_height
    output = [
        f'<line class="axis" x1="{chart_x}" y1="{chart_bottom}" '
        f'x2="{chart_x + chart_width}" y2="{chart_bottom}" />'
    ]

    if not points:
        output.append(
            '<text class="empty" x="180" y="120" text-anchor="middle">'
            "Collecting star history</text>"
        )
    else:
        values = [int(point.get("total_stargazers", 0)) for point in points]
        min_value = min(values)
        max_value = max(values)
        if min_value == max_value:
            min_value -= 1
            max_value += 1
        value_range = max(max_value - min_value, 1)
        day_range = max(int(window.get("days", 1)) - 1, 1)
        coordinates = []
        for point in points:
            offset_days = max(0, min(day_range, int(point.get("offset_days", 0))))
            value = int(point.get("total_stargazers", 0))
            x = chart_x + ((offset_days / day_range) * chart_width)
            y = chart_y + (((max_value - value) / value_range) * chart_height)
            coordinates.append((x, y, point))

        if len(coordinates) > 1:
            path = " ".join(
                f"{'M' if index == 0 else 'L'} {x:.1f} {y:.1f}"
                for index, (x, y, _point) in enumerate(coordinates)
            )
            output.append(f'<path class="chart-line" d="{path}" />')

        for index, (x, y, point) in enumerate(coordinates):
            total = int(point.get("total_stargazers", 0))
            date_value = str(point.get("date", ""))
            output.append(
                f'<circle class="chart-point" cx="{x:.1f}" cy="{y:.1f}" '
                f'r="3.5" style="animation-delay: {index * 55}ms">'
                f"<title>{_svg_text(date_value)}: {total:,} stars</title>"
                "</circle>"
            )

        endpoints = [coordinates[0]]
        if len(coordinates) > 1:
            endpoints.append(coordinates[-1])
        center_x = chart_x + (chart_width / 2)
        for endpoint_index, (x, y, point) in enumerate(endpoints):
            if len(endpoints) == 1:
                text_anchor = "end" if x > center_x else "start"
            elif endpoint_index == 0:
                text_anchor = "start" if x <= center_x else "end"
            else:
                text_anchor = "end" if x >= center_x else "start"
            label_offset = -6 if text_anchor == "end" else 6
            label_x = max(21, min(339, x + label_offset))
            label_y = max(68, min(chart_bottom - 8, y - 8))
            total = int(point.get("total_stargazers", 0))
            output.append(
                f'<text class="endpoint-label" x="{label_x:.1f}" '
                f'y="{label_y:.1f}" text-anchor="{text_anchor}">'
                f"{total:,}</text>"
            )

    latest_total = _format_number(window.get("latest_total", 0))
    delta = _format_signed_number(window.get("delta", 0))
    sample_count = _format_number(window.get("sample_count", 0))
    output.append(
        '<g class="summary-row">'
        '<text class="label" x="21" y="181">Latest</text>'
        f'<text class="value" x="106" y="181" text-anchor="end">{latest_total}</text>'
        '<text class="label" x="141" y="181">Change</text>'
        f'<text class="value" x="229" y="181" text-anchor="end">{delta}</text>'
        '<text class="label" x="265" y="181">Samples</text>'
        f'<text class="value" x="339" y="181" text-anchor="end">{sample_count}</text>'
        "</g>"
    )
    return "\n".join(output)


def _generate_experimental_contribution_pulse(metrics: Dict[str, Any]) -> None:
    pulse = metrics["contribution_pulse"]
    rows = [
        ("Total commits shown", _format_number(pulse["total"])),
        ("Active months", f'{pulse["active_months"]}/{pulse["month_count"]}'),
        ("Average per month", pulse["average_monthly"]),
        ("Best month", f'{pulse["best_month"]} ({_format_number(pulse["best_month_count"])})'),
        (
            "Current month",
            f'{pulse["current_month"]} ({_format_number(pulse["current_month_count"])})',
        ),
        ("Active streak", f'{pulse["active_streak_months"]} months'),
    ]
    _render_experimental_template(
        "contribution-pulse",
        "Contribution Pulse",
        "Recent activity — last 13 months",
        _experimental_rows(rows),
    )


def _generate_experimental_contribution_mix(metrics: Dict[str, Any]) -> None:
    mix = metrics["contribution_mix"]
    contribution_items = [
        ("Commits", int(mix["commits"]), "#2da44e"),
        ("Pull requests", int(mix["pull_requests"]), "#0969da"),
        ("Reviews", int(mix["pull_request_reviews"]), "#8250df"),
        ("Issues", int(mix["issues"]), "#bf8700"),
        ("Repositories", int(mix["repositories"]), "#1f6feb"),
        ("Restricted", int(mix["restricted"]), "#8b949e"),
    ]
    sorted_items = [
        item
        for _, item in sorted(
            enumerate(contribution_items),
            key=lambda indexed_item: (-indexed_item[1][1], indexed_item[0]),
        )
    ]
    stack = _experimental_stack_bar(sorted_items)
    rows = _experimental_rows(
        [(label, _format_number(value)) for label, value, _ in sorted_items],
        start_y=98,
        row_gap=16,
    )
    _render_experimental_template(
        "contribution-mix",
        "Contribution Mix",
        f'Current year total: {_format_number(mix["total"])}',
        stack + "\n" + rows,
    )


def _generate_experimental_impact(metrics: Dict[str, Any]) -> None:
    impact = metrics["impact"]
    top_repo = impact["top_public_repo"]
    rows = [
        ("Stars", _format_number(impact["stars"])),
        ("Forks", _format_number(impact["forks"])),
        ("Views, 14 days", _format_number(impact["views_14_days"])),
        ("Clones, 14 days", _format_number(impact["clones_14_days"])),
        ("Top public repo", top_repo["display_name"]),
        ("Top repo score", _format_number(top_repo["score"])),
    ]
    _render_experimental_template(
        "impact",
        "Impact",
        f"Traffic is time-limited; {_limited_suffix(metrics)}",
        _experimental_rows(rows),
    )


def _generate_experimental_language_momentum(metrics: Dict[str, Any]) -> None:
    items = [
        {
            "language": item["language"],
            "percent": f'{item["percent"]:0.1f}%',
            "raw_percent": item["percent"],
            "display_value": f'{item["percent"]:0.1f}%',
            "color": item["color"],
        }
        for item in metrics["language_momentum"]
    ]
    bars = _experimental_horizontal_bars(
        items,
        "language",
        "raw_percent",
        color_key="color",
        bar_x=182,
        bar_max_width=96,
        value_x=170,
        value_position="before_bar",
        label_icon_key="language",
        label_icon_color_key="color",
    )
    _render_experimental_template(
        "language-momentum",
        "Language Momentum",
        "Estimated from recent repo activity",
        bars,
    )


def _generate_experimental_collaboration(metrics: Dict[str, Any]) -> None:
    collaboration = metrics["collaboration"]
    rows = [
        ("Merged pull requests", _format_number(collaboration["merged_pull_requests"])),
        ("PR reviews", _format_number(collaboration["pull_request_reviews"])),
        ("Issues opened", _format_number(collaboration["issues"])),
        ("Owned repositories", _format_number(collaboration["owned_repositories"])),
        (
            "External repositories",
            _format_number(collaboration["external_repositories"]),
        ),
    ]
    _render_experimental_template(
        "collaboration",
        "Collaboration",
        "Current-year contribution signals",
        _experimental_rows(rows),
    )


def _generate_experimental_maintainer_health(metrics: Dict[str, Any]) -> None:
    health = metrics["maintainer_health"]
    rows = [
        ("Open issues", _format_number(health["open_issues"])),
        ("Open pull requests", _format_number(health["open_pull_requests"])),
        ("Updated in 90 days", _format_number(health["recently_updated"])),
        ("Stale over 1 year", _format_number(health["stale_repositories"])),
        ("Archived", _format_number(health["archived_repositories"])),
        ("With releases", _format_number(health["release_repositories"])),
        ("With tags", _format_number(health["tagged_repositories"])),
    ]
    _render_experimental_template(
        "maintainer-health",
        "Maintainer Health",
        "Repository maintenance signals",
        _experimental_rows(rows),
    )


def _generate_experimental_personal_bests(metrics: Dict[str, Any]) -> None:
    bests = metrics["personal_bests"]
    rows = [
        (
            "Best month",
            f'{bests["best_month"]} ({_format_number(bests["best_month_count"])})',
        ),
        ("Active streak", f'{bests["active_streak_months"]} months'),
        ("Top public repo", bests["top_public_repo"]),
        ("Top repo score", _format_number(bests["top_public_repo_score"])),
        ("Private repos redacted", metrics["privacy"]["private_repositories"]),
    ]
    _render_experimental_template(
        "personal-bests",
        "Personal Bests",
        "Highlights from available profile data",
        _experimental_rows(rows),
    )


def _generate_experimental_trading_card(metrics: Dict[str, Any]) -> None:
    card = metrics["trading_card"]
    rows = [
        ("Primary language", card["primary_language"]),
        ("Activity score", f'{card["activity_score"]}/100'),
        ("Impact score", f'{card["impact_score"]}/100'),
        ("Collaboration score", f'{card["collaboration_score"]}/100'),
        ("Breadth score", f'{card["breadth_score"]}/100'),
    ]
    _render_experimental_template(
        "trading-card",
        "GitHub Trading Card",
        "Transparent experimental scorecard",
        _experimental_rows(rows, value_icon_labels={"Primary language"}),
    )


def _generate_experimental_repo_portfolio(metrics: Dict[str, Any]) -> None:
    portfolio = metrics["repo_portfolio"]
    bars = _experimental_horizontal_bars(
        portfolio["repositories"],
        "display_name",
        "score",
        label_max_chars=23,
        value_max_chars=8,
        bar_x=182,
        bar_max_width=96,
        value_x=330,
    )
    _render_experimental_template(
        "repo-portfolio",
        "Repo Portfolio",
        (
            f'{portfolio["public_repositories"]} public; '
            f'{portfolio["private_repositories"]} private redacted'
        ),
        bars,
    )


def _generate_experimental_commit_velocity(metrics: Dict[str, Any]) -> None:
    velocity = metrics["commit_velocity"]
    _render_experimental_template(
        "commit-velocity",
        "Commit Velocity",
        "Exact trailing averages",
        _commit_velocity_table(velocity["windows"]),
    )


def _generate_experimental_star_history(metrics: Dict[str, Any]) -> None:
    for window in metrics["star_history"]["windows"]:
        card_name = f"star-history-{str(window['key']).replace('_', '-')}"
        if int(window.get("sample_count", 0)) > 0:
            subtitle = (
                f"{_format_number(window['latest_total'])} total; "
                f"{_format_signed_number(window['delta'])} in window"
            )
        else:
            subtitle = "Collecting daily star snapshots"
        _render_star_history_template(
            card_name,
            f"Stars: {window['label']}",
            subtitle,
            _star_history_chart(window),
        )


async def generate_experimental(s: Stats) -> Dict[str, Any]:
    metrics = await build_experimental_metrics(s)
    _generate_experimental_contribution_pulse(metrics)
    _generate_experimental_contribution_mix(metrics)
    _generate_experimental_impact(metrics)
    _generate_experimental_language_momentum(metrics)
    _generate_experimental_collaboration(metrics)
    _generate_experimental_maintainer_health(metrics)
    _generate_experimental_personal_bests(metrics)
    _generate_experimental_trading_card(metrics)
    _generate_experimental_repo_portfolio(metrics)
    _generate_experimental_commit_velocity(metrics)
    _generate_experimental_star_history(metrics)
    return metrics


################################################################################
# Main Function
################################################################################


async def main() -> None:
    """
    Generate all badges
    """
    load_env_file()
    access_token = os.getenv("ACCESS_TOKEN")
    if not access_token:
        # access_token = os.getenv("GITHUB_TOKEN")
        raise Exception("A personal access token is required to proceed!")
    user = os.getenv("GITHUB_ACTOR")
    if user is None:
        raise RuntimeError("Environment variable GITHUB_ACTOR must be set.")
    exclude_repos = os.getenv("EXCLUDED")
    excluded_repos = (
        {x.strip() for x in exclude_repos.split(",")} if exclude_repos else None
    )
    exclude_langs = os.getenv("EXCLUDED_LANGS")
    excluded_langs = (
        {x.strip() for x in exclude_langs.split(",")} if exclude_langs else None
    )
    # Convert a truthy value to a Boolean
    raw_ignore_forked_repos = os.getenv("EXCLUDE_FORKED_REPOS")
    ignore_forked_repos = (
        not not raw_ignore_forked_repos
        and raw_ignore_forked_repos.strip().lower() != "false"
    )
    async with aiohttp.ClientSession() as session:
        s = Stats(
            user,
            access_token,
            session,
            exclude_repos=excluded_repos,
            exclude_langs=excluded_langs,
            ignore_forked_repos=ignore_forked_repos,
        )
        await s.get_stats()
        record_star_history_sample(
            await s.stargazers,
            len(await s.repos),
        )
        await asyncio.gather(
            generate_languages(s),
            generate_overview(s),
        )
        await generate_monthly_commits(s)
        if env_truthy("GENERATE_EXPERIMENTAL", default=True):
            await generate_experimental(s)
        validate_generated_output()
        report = await build_run_report(s)
        write_run_report(report)
        write_action_summary(render_action_summary(report))
        validate_run_report(report)


if __name__ == "__main__":
    asyncio.run(main())
