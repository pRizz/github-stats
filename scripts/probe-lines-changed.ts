#!/usr/bin/env bun

type ContributorWeek = {
  w?: number;
  a?: number;
  d?: number;
  c?: number;
};

type ContributorStats = {
  total?: number;
  author?: {
    login?: string;
  } | null;
  weeks?: ContributorWeek[];
};

type Options = {
  repo: string;
  maybeUsername?: string;
  timeoutSeconds: number;
  intervalSeconds: number;
};

type Totals = {
  additions: number;
  deletions: number;
  changed: number;
  commits: number;
  weeks: number;
};

const DEFAULT_TIMEOUT_SECONDS = 3600;
const DEFAULT_INTERVAL_SECONDS = 30;

function usage(exitCode = 2): never {
  console.error(`Usage:
  bun scripts/probe-lines-changed.ts OWNER/REPO [USERNAME] [--timeout 3600] [--interval 30]

Environment:
  ACCESS_TOKEN, GITHUB_TOKEN, or GH_TOKEN must contain a GitHub token.

Examples:
  ACCESS_TOKEN=... bun scripts/probe-lines-changed.ts pRizz/github-stats pRizz
  GITHUB_TOKEN=... bun scripts/probe-lines-changed.ts pRizz/github-stats --interval 60
`);
  process.exit(exitCode);
}

function readArgs(argv: string[]): Options {
  const positional: string[] = [];
  let timeoutSeconds = DEFAULT_TIMEOUT_SECONDS;
  let intervalSeconds = DEFAULT_INTERVAL_SECONDS;

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--timeout") {
      const value = argv[index + 1];
      if (!value) usage();
      timeoutSeconds = Number(value);
      index += 1;
      continue;
    }
    if (arg === "--interval") {
      const value = argv[index + 1];
      if (!value) usage();
      intervalSeconds = Number(value);
      index += 1;
      continue;
    }
    if (arg === "--help" || arg === "-h") {
      usage(0);
    }
    positional.push(arg);
  }

  const repo = positional[0] ?? process.env.GITHUB_REPOSITORY;
  const maybeUsername = positional[1] ?? process.env.GITHUB_ACTOR;
  if (!repo || !repo.includes("/")) usage();
  if (!Number.isFinite(timeoutSeconds) || timeoutSeconds <= 0) usage();
  if (!Number.isFinite(intervalSeconds) || intervalSeconds <= 0) usage();

  return { repo, maybeUsername, timeoutSeconds, intervalSeconds };
}

function tokenInfo(): { token: string; source: string } {
  for (const source of ["ACCESS_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"]) {
    const token = process.env[source];
    if (token) return { token, source };
  }

  console.error("Missing GitHub token. Set ACCESS_TOKEN, GITHUB_TOKEN, or GH_TOKEN.");
  process.exit(2);
}

function sumWeeks(weeks: ContributorWeek[] = []): Totals {
  const totals = weeks.reduce(
    (acc, week) => {
      acc.additions += week.a ?? 0;
      acc.deletions += week.d ?? 0;
      acc.commits += week.c ?? 0;
      return acc;
    },
    { additions: 0, deletions: 0, changed: 0, commits: 0, weeks: weeks.length },
  );
  totals.changed = totals.additions + totals.deletions;
  return totals;
}

function summarizePayload(payload: unknown, maybeUsername?: string) {
  if (!Array.isArray(payload)) {
    return { payload_type: typeof payload };
  }

  const contributors = payload as ContributorStats[];
  const all = contributors.reduce(
    (acc, contributor) => {
      const totals = sumWeeks(contributor.weeks ?? []);
      acc.additions += totals.additions;
      acc.deletions += totals.deletions;
      acc.commits += totals.commits;
      acc.weeks += totals.weeks;
      return acc;
    },
    { additions: 0, deletions: 0, changed: 0, commits: 0, weeks: 0 },
  );
  all.changed = all.additions + all.deletions;

  const maybeContributor = maybeUsername
    ? contributors.find((item) => item.author?.login === maybeUsername)
    : undefined;

  return {
    payload_type: "contributors",
    contributor_count: contributors.length,
    all_contributors: all,
    target_user: maybeUsername
      ? {
          login: maybeUsername,
          found: Boolean(maybeContributor),
          ...(maybeContributor ? sumWeeks(maybeContributor.weeks ?? []) : {}),
          total_commits_reported: maybeContributor?.total ?? null,
        }
      : null,
  };
}

function headerObject(headers: Headers): Record<string, string | null> {
  return {
    "retry-after": headers.get("retry-after"),
    "x-github-request-id": headers.get("x-github-request-id"),
    "x-ratelimit-limit": headers.get("x-ratelimit-limit"),
    "x-ratelimit-remaining": headers.get("x-ratelimit-remaining"),
    "x-ratelimit-reset": headers.get("x-ratelimit-reset"),
    "x-ratelimit-used": headers.get("x-ratelimit-used"),
    "x-oauth-scopes": headers.get("x-oauth-scopes"),
    "x-accepted-oauth-scopes": headers.get("x-accepted-oauth-scopes"),
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function sleepWithProgress(seconds: number, startedAtMs: number): Promise<void> {
  const waitStarted = Date.now();
  const waitMs = seconds * 1000;
  let nextLogMs = 0;

  while (Date.now() - waitStarted < waitMs) {
    const waitedMs = Date.now() - waitStarted;
    if (waitedMs >= nextLogMs) {
      const waitedSeconds = Math.floor(waitedMs / 1000);
      const elapsedSeconds = ((Date.now() - startedAtMs) / 1000).toFixed(1);
      console.log(
        `[wait] ${waitedSeconds}/${seconds.toFixed(1)}s before next query ` +
          `(elapsed ${elapsedSeconds}s)`,
      );
      nextLogMs += 5000;
    }
    await sleep(Math.min(1000, waitMs - waitedMs));
  }
}

async function readJsonOrText(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function main(): Promise<void> {
  const options = readArgs(Bun.argv.slice(2));
  const { token, source } = tokenInfo();
  const endpoint = `https://api.github.com/repos/${options.repo}/stats/contributors`;
  const startedAt = new Date();
  const startedAtMs = startedAt.getTime();
  const deadlineMs = startedAtMs + options.timeoutSeconds * 1000;
  let attempts = 0;
  let totalWaitSeconds = 0;
  let lastStatus = 0;
  let lastHeaders: Record<string, string | null> = {};
  let payload: unknown = null;
  let exitReason = "timeout";

  console.log(
    `[start] repo=${options.repo} user=${options.maybeUsername ?? "(all)"} ` +
      `timeout=${options.timeoutSeconds}s interval=${options.intervalSeconds}s ` +
      `token_source=${source}`,
  );

  while (Date.now() <= deadlineMs) {
    attempts += 1;
    const attemptStartedMs = Date.now();
    const elapsedSeconds = ((attemptStartedMs - startedAtMs) / 1000).toFixed(1);
    console.log(`[query] attempt=${attempts} elapsed=${elapsedSeconds}s ${endpoint}`);

    const response = await fetch(endpoint, {
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-stats-lines-changed-probe",
      },
    });

    lastStatus = response.status;
    lastHeaders = headerObject(response.headers);
    payload = await readJsonOrText(response);

    const requestSeconds = ((Date.now() - attemptStartedMs) / 1000).toFixed(2);
    console.log(
      `[response] attempt=${attempts} status=${response.status} ` +
        `request_seconds=${requestSeconds} request_id=${lastHeaders["x-github-request-id"]}`,
    );

    if (response.status !== 202) {
      exitReason = "non_202_response";
      break;
    }

    const remainingSeconds = Math.max(0, (deadlineMs - Date.now()) / 1000);
    if (remainingSeconds <= 0) {
      console.log("[timeout] no time remains before the next retry");
      break;
    }

    const retryAfter = Number(response.headers.get("retry-after"));
    const waitSeconds = Math.min(
      Number.isFinite(retryAfter) && retryAfter > 0
        ? Math.max(options.intervalSeconds, retryAfter)
        : options.intervalSeconds,
      remainingSeconds,
    );
    totalWaitSeconds += waitSeconds;
    await sleepWithProgress(waitSeconds, startedAtMs);
  }

  const finishedAt = new Date();
  const elapsedSeconds = (finishedAt.getTime() - startedAtMs) / 1000;
  const summary = {
    metadata: {
      repo: options.repo,
      endpoint,
      target_user: options.maybeUsername ?? null,
      started_at: startedAt.toISOString(),
      finished_at: finishedAt.toISOString(),
      timeout_seconds: options.timeoutSeconds,
      interval_seconds: options.intervalSeconds,
      token_source: source,
    },
    result: {
      status: lastStatus,
      completed: lastStatus !== 202,
      timed_out: lastStatus === 202,
      exit_reason: exitReason,
      attempts,
      elapsed_seconds: Number(elapsedSeconds.toFixed(1)),
      total_wait_seconds: totalWaitSeconds,
    },
    response_metadata: lastHeaders,
    metrics: lastStatus === 200 ? summarizePayload(payload, options.maybeUsername) : null,
    response_preview:
      lastStatus === 200
        ? null
        : typeof payload === "string"
          ? payload.slice(0, 500)
          : payload,
  };

  console.log("[summary]");
  console.log(JSON.stringify(summary, null, 2));
}

await main();
