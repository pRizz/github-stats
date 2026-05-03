## lesson-generated-svg-rebase-conflicts | 2026-05-03 00:00

1. Date: 2026-05-03
2. What went wrong: A rebase stopped on generated file conflicts, and I treated all generated conflicts as a blocker instead of applying the repo's intended generated-SVG policy.
3. Preventive rule: If every conflicted file is a regenerated SVG under `generated/`, keep the local commit's regenerated SVG output during the rebase, then rerun relevant validation when practical.
4. Trigger signal to catch it earlier: `git diff --name-only --diff-filter=U` lists only `generated/*.svg` files during a rebase.
