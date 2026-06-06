## lesson-generated-svg-rebase-conflicts | 2026-05-03 00:00

1. Date: 2026-05-03
2. What went wrong: A rebase stopped on generated file conflicts, and I treated all generated conflicts as a blocker instead of applying the repo's intended generated-SVG policy.
3. Preventive rule: If every conflicted file is a regenerated SVG under `generated/`, keep the local commit's regenerated SVG output during the rebase, then rerun relevant validation when practical.
4. Trigger signal to catch it earlier: `git diff --name-only --diff-filter=U` lists only `generated/*.svg` files during a rebase.

## lesson-separate-repo-scopes | 2026-06-06 14:56

1. Date: 2026-06-06
2. What went wrong: Expanding repository discovery for monthly commit scans also changed shared star and impact aggregates, so external contributed repositories were treated like owned repositories.
3. Preventive rule: When changing repository discovery, keep metric scopes explicit and test owned, external, and forked repositories separately before reusing a shared aggregate.
4. Trigger signal to catch it earlier: A non-owned high-star repository appears in generated star totals, impact top repository, or repo portfolio output after a contribution-scan change.
