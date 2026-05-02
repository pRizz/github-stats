#!/usr/bin/python3

import argparse
import asyncio
import os
import sys

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
    return parser.parse_args()


async def main() -> None:
    args = read_args()
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
        await generate_images.generate_monthly_commits(
            stats,
            force_backfill=args.force,
        )


if __name__ == "__main__":
    asyncio.run(main())
