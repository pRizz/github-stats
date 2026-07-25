#!/usr/bin/python3

import argparse
import asyncio
import os
import sys
from typing import Any, Dict

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_images
from github_stats import Stats


def read_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill the committed monthly commit-count cache and SVG."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recompute all 13 months instead of refreshing only missing/current data.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print the backfill diff without writing generated files.",
    )
    return parser.parse_args()


def render_backfill_diff(
    existing: Dict[str, Any],
    candidate: Dict[str, Any],
) -> str:
    """Render a privacy-safe comparison of committed and candidate counts."""
    existing_by_key = {
        item["key"]: item
        for item in existing.get("months", [])
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    }
    discovery = candidate.get("source", {}).get("discovery", {})
    lines = [
        "Month\tExisting\tCandidate\tGitHub attributed\tStatus",
    ]
    for item in candidate.get("months", []):
        key = item.get("key", "")
        old_count = int(existing_by_key.get(key, {}).get("count", 0))
        candidate_count = int(
            item.get("scan_candidate_count", item.get("count", 0))
        )
        attributed_count = int(item.get("github_attributed_count", 0))
        if item.get("scan_degraded"):
            status = "incomplete"
        elif candidate_count == old_count:
            status = "unchanged"
        else:
            status = "changed"
        lines.append(
            f"{key}\t{old_count}\t{candidate_count}\t"
            f"{attributed_count}\t{status}"
        )

    lines.extend(
        [
            "",
            f"Base repositories: {discovery.get('base_repo_count', 0)}",
            "Contribution repositories discovered: "
            f"{discovery.get('contribution_repo_count', 0)}",
            f"Scan repositories: {discovery.get('scan_repo_count', 0)}",
            "Incomplete months: "
            + (
                ", ".join(discovery.get("incomplete_months", []))
                if discovery.get("incomplete_months")
                else "none"
            ),
        ]
    )
    return "\n".join(lines)


async def main() -> None:
    args = read_args()
    if args.dry_run and not args.force:
        raise RuntimeError("--dry-run requires --force.")

    access_token = os.getenv("ACCESS_TOKEN")
    if not access_token:
        raise RuntimeError("ACCESS_TOKEN must be set.")

    user = os.getenv("GITHUB_ACTOR")
    if user is None:
        raise RuntimeError("GITHUB_ACTOR must be set.")

    exclude_repos = os.getenv("EXCLUDED")
    excluded_repos = (
        {item.strip() for item in exclude_repos.split(",") if item.strip()}
        if exclude_repos
        else None
    )
    exclude_langs = os.getenv("EXCLUDED_LANGS")
    excluded_langs = (
        {item.strip() for item in exclude_langs.split(",") if item.strip()}
        if exclude_langs
        else None
    )
    raw_ignore_forked_repos = os.getenv("EXCLUDE_FORKED_REPOS")
    ignore_forked_repos = (
        not not raw_ignore_forked_repos
        and raw_ignore_forked_repos.strip().lower() != "false"
    )

    async with aiohttp.ClientSession() as session:
        stats = Stats(
            user,
            access_token,
            session,
            exclude_repos=excluded_repos,
            exclude_langs=excluded_langs,
            ignore_forked_repos=ignore_forked_repos,
        )
        await stats.get_stats()
        if args.dry_run:
            existing = generate_images.load_monthly_commits_cache()
            candidate = await generate_images.build_monthly_commit_cache(
                stats,
                force_backfill=True,
                write_cache=False,
            )
            print(render_backfill_diff(existing, candidate))
            degraded_months = int(
                candidate.get("source", {}).get("degraded_months", 0)
            )
            if degraded_months > 0:
                raise RuntimeError(
                    "Dry-run found incomplete monthly data; no files were written."
                )
            return

        await generate_images.generate_monthly_commits(
            stats,
            force_backfill=args.force,
        )


if __name__ == "__main__":
    asyncio.run(main())
