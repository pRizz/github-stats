# Repo Guidance

## GitHub Contributor Stats Endpoint

Do not reintroduce GitHub's `/repos/{owner}/{repo}/stats/contributors`
endpoint into generation, SVG rendering, action summaries, default local
debugging, or cached report data.

The endpoint can remain `202 Accepted` after extended waiting. Local testing
found five sampled repositories still pending after a 30-minute poll every 7
seconds, following an earlier roughly 20-minute once-per-minute poll. Treat
line additions, line deletions, churn, and derived line-change metrics as
omitted data.

Use commit-count scans, GraphQL `ContributionsCollection` totals, repository
metadata, traffic data, or other bounded APIs for future generated metrics.

## Generated SVG Rebase Conflicts

When rebasing, if every conflicted file is a regenerated SVG under `generated/`,
resolve those SVG conflicts by keeping the local commit's regenerated output.
In Git rebase conflict terminology this may be `--theirs`, not `--ours`, because
the commit being replayed is the local change. Re-run the relevant generation or
validation command afterward when practical.

Do not use this shortcut when conflicts include source code, JSON cache/report
files, templates, documentation, or any non-SVG artifact. Resolve those conflicts
intentionally or report the blocker.
