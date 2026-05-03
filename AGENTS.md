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

## Generated Artifact Rebase Conflicts

When rebasing, if every conflicted file is a regenerated artifact under
`generated/`, resolve those conflicts by keeping the local commit's regenerated
output. This applies to generated SVG and JSON artifacts. In Git rebase conflict
terminology this may be `--theirs`, not `--ours`, because the commit being
replayed is the local change.

Use this shortcut only when the files that produce the generated output are not
also conflicted. Do not use it if conflicts include source code, templates,
vendored assets, documentation, repo guidance, or any other generator input.

After resolving generated artifacts mechanically, re-run the relevant generation
command when practical, then run validation before continuing. If regeneration
requires unavailable credentials or network access, run the strongest available
validation and report the limitation.
