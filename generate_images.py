#!/usr/bin/python3

import asyncio
import datetime as dt
import html
import json
import os
import re
from typing import Any, Dict, List, Optional, Set

import aiohttp

from github_stats import (
    ApiDegradation,
    ApiWaitStat,
    MonthWindow,
    MonthlyCommitRecord,
    Stats,
    monthly_commit_identity_patterns,
    rolling_month_windows,
)


MONTHLY_COMMITS_CACHE = os.path.join("generated", "monthly-commits.json")


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
    contributor_items = [
        ApiDegradation(**item) for item in api["contributors_degraded"]
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

## API Degradations

- Contributor stats degraded: {len(contributor_items)}
- Traffic views degraded: {len(traffic_items)}
- Monthly commit scans degraded: {len(monthly_commit_items)}

## API Wait Summary

- REST requests: {api["rest_requests_total"]:,}
- REST retries: {api["rest_retries_total"]:,}
- Total retry wait: {api["rest_wait_seconds_total"]:.1f}s
- Contributor stats retry wait: {api["contributors_wait_seconds_total"]:.1f}s
- Traffic views retry wait: {api["traffic_wait_seconds_total"]:.1f}s
- Monthly commit scan retry wait: {api.get("monthly_commits_wait_seconds_total", 0.0):.1f}s

### Slowest API Waits

{_format_wait_items(slowest_items)}
### Traffic View Failures

{_format_degradation_items(traffic_items)}
### Contributor Stats Warnings

{_format_degradation_items(contributor_items)}
### Monthly Commit Scan Warnings

{_format_degradation_items(monthly_commit_items)}
"""


def validate_run_report(report: Dict[str, Any]) -> None:
    """
    Fail when optional REST metrics degraded into misleading all-zero values.
    """
    stats = report["stats"]
    api = report["api"]
    repo_count = stats["repos"]
    failures = []

    traffic_degraded_count = len(api["traffic_degraded"])

    if repo_count > 0 and stats["views"] == 0 and traffic_degraded_count > 0:
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
    months = monthly_cache.get("months", [])
    current_month = next(
        (item for item in months if item.get("is_current")),
        {},
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
<svg xmlns="http://www.w3.org/2000/svg" class="octicon" style="fill:{color};"
viewBox="0 0 16 16" version="1.1" width="16" height="16"><path
fill-rule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8z"></path></svg>
<span class="lang">{lang}</span>
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


################################################################################
# Main Function
################################################################################


async def main() -> None:
    """
    Generate all badges
    """
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
        await asyncio.gather(
            generate_languages(s),
            generate_overview(s),
            generate_monthly_commits(s),
        )
        validate_generated_output()
        report = await build_run_report(s)
        write_run_report(report)
        write_action_summary(render_action_summary(report))
        validate_run_report(report)


if __name__ == "__main__":
    asyncio.run(main())
