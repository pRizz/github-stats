#!/usr/bin/python3

import asyncio
import json
import os
import re
from typing import Any, Dict, List

import aiohttp

from github_stats import ApiDegradation, ApiWaitStat, Stats


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

    with open(overview_path, "r") as f:
        overview = f.read()
    with open(languages_path, "r") as f:
        languages = f.read()

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

    if overview_is_empty_fallback or languages_are_empty:
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
    slowest_items = [
        ApiWaitStat(**item) for item in api["slowest_requests"]
    ]

    return f"""# GitHub Stats Image Generation

## Generated Stats

- Name: {stats["name"]}
- Stargazers: {stats["stargazers"]:,}
- Forks: {stats["forks"]:,}
- All-time contributions: {stats["total_contributions"]:,}
- Current-year contributions: {stats["current_year_contributions"]:,}
- Merged pull requests: {stats["merged_pull_requests"]:,}
- Repositories: {stats["repos"]:,}
- Repository views: {stats["views"]:,}

## API Degradations

- Contributor stats degraded: {len(contributor_items)}
- Traffic views degraded: {len(traffic_items)}

## API Wait Summary

- REST requests: {api["rest_requests_total"]:,}
- REST retries: {api["rest_retries_total"]:,}
- Total retry wait: {api["rest_wait_seconds_total"]:.1f}s
- Contributor stats retry wait: {api["contributors_wait_seconds_total"]:.1f}s
- Traffic views retry wait: {api["traffic_wait_seconds_total"]:.1f}s

### Slowest API Waits

{_format_wait_items(slowest_items)}
### Traffic View Failures

{_format_degradation_items(traffic_items)}
### Contributor Stats Warnings

{_format_degradation_items(contributor_items)}
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
        await asyncio.gather(generate_languages(s), generate_overview(s))
        validate_generated_output()
        report = await build_run_report(s)
        write_run_report(report)
        write_action_summary(render_action_summary(report))
        validate_run_report(report)


if __name__ == "__main__":
    asyncio.run(main())
