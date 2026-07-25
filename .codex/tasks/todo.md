## task-monthly-commit-repair | 2026-07-25 13:44 | Repair and backfill monthly commit statistics

- [x] Fast-forward the clean local `master` branch.
- [x] Add monthly-only repository discovery and completeness validation.
- [x] Preserve cached current-month counts on incomplete or regressing refreshes.
- [x] Add atomic dry-run and force-backfill behavior with reporting.
- [x] Enable strict workflow validation and action-summary warnings.
- [x] Add regression and metric-scope isolation coverage.
- [x] Run the full unit suite and generated-output validation.
- [ ] Run and review a live 13-month dry-run with a minimally scoped token.
- [ ] Apply a validated backfill and regenerate dependent artifacts if all months are complete.
- [x] Review the final diff and record residual risks.

Completion review, 2026-07-25 14:20 CDT:

- The 13-month GraphQL discovery smoke test is complete for every month, and July now discovers 10 contribution repositories with no coverage gap.
- Python 3.8 and local-environment suites pass with 98 tests; workflow lint, bytecode compilation, generated-output validation, and diff checks pass.
- Residual blocker: the only available local credentials are broader than the approved rollout scope. A minimally scoped token is still required before the full REST dry-run, artifact backfill, Actions secret update, and manual workflow dispatch.
