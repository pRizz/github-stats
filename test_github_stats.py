#!/usr/bin/python3

import asyncio
import datetime as dt
import unittest
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import requests

import github_stats
import generate_images
from github_stats import Queries, Stats


class _FakeResponse:
    def __init__(self, status, payload=None, json_error=None, text=""):
        self.status = status
        self.status_code = status
        self._payload = payload
        self._json_error = json_error
        self.text = text
        self.headers = {}

    async def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload

    def json_sync(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _FakeRequestContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False


class _FakeSession:
    def __init__(self, response):
        self.response = response

    def post(self, *args, **kwargs):
        return _FakeRequestContext(self.response)

    def get(self, *args, **kwargs):
        return _FakeRequestContext(self.response)


class _FailingSession:
    def post(self, *args, **kwargs):
        raise RuntimeError("network failed")

    def get(self, *args, **kwargs):
        raise RuntimeError("network failed")


class _FakeRequestsResponse:
    def __init__(
        self,
        status_code=502,
        payload=None,
        json_error=None,
        text="Bad Gateway",
        headers=None,
    ):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error
        self.text = text
        self.headers = {} if headers is None else headers

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        if self._payload is not None:
            return self._payload
        raise requests.exceptions.JSONDecodeError("Expecting value", "", 0)


class _Always202Response:
    status_code = 202
    text = ""
    headers = {}


class _ConcurrentContributorQueries:
    def __init__(self):
        self.active_requests = 0
        self.max_active_requests = 0

    async def query_rest_with_outcome(self, path, params=None, max_wait_seconds=None):
        self.active_requests += 1
        self.max_active_requests = max(self.max_active_requests, self.active_requests)
        try:
            await generate_images.asyncio.sleep(0.01)
            return github_stats.RestQueryResult(
                [
                    {
                        "author": {"login": "octocat"},
                        "weeks": [{"a": 10, "d": 4}],
                    }
                ],
                attempts=2,
                retry_count=1,
                wait_seconds=0.5,
                elapsed_seconds=0.6,
            )
        finally:
            self.active_requests -= 1


class _PathOutcomeQueries:
    def __init__(self, outcomes):
        self.outcomes = outcomes

    async def query_rest_with_outcome(self, path, params=None, max_wait_seconds=None):
        return self.outcomes[path]


class _MonthlyCommitQueries:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []
        self.active_requests = 0
        self.max_active_requests = 0

    async def query_rest_with_outcome(self, path, params=None, max_wait_seconds=None):
        params = {} if params is None else params
        self.calls.append((path, dict(params)))
        self.active_requests += 1
        self.max_active_requests = max(self.max_active_requests, self.active_requests)
        try:
            return self.outcomes[(path, params.get("page", "1"))]
        finally:
            self.active_requests -= 1


class _SlowMonthlyCommitQueries(_MonthlyCommitQueries):
    async def query_rest_with_outcome(self, path, params=None, max_wait_seconds=None):
        params = {} if params is None else params
        self.calls.append((path, dict(params)))
        self.active_requests += 1
        self.max_active_requests = max(self.max_active_requests, self.active_requests)
        try:
            await asyncio.sleep(0.01)
            return self.outcomes[(path, params.get("page", "1"))]
        finally:
            self.active_requests -= 1


class _FakeStats:
    loaded = False
    get_stats_calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def get_stats(self):
        type(self).loaded = True
        type(self).get_stats_calls += 1


class _FailingStats:
    def __init__(self, *args, **kwargs):
        pass

    async def get_stats(self):
        raise RuntimeError("critical stats unavailable")


class _OverviewStats:
    lines_changed_accessed = False

    @property
    async def name(self):
        return "Octocat"

    @property
    async def stargazers(self):
        return 1

    @property
    async def forks(self):
        return 2

    @property
    async def total_contributions(self):
        return 3

    @property
    async def merged_pull_requests(self):
        return 42

    @property
    async def lines_changed(self):
        type(self).lines_changed_accessed = True
        raise AssertionError("overview should not read lines_changed")

    @property
    async def views(self):
        return 4

    @property
    async def repos(self):
        return {"octocat/hello", "octocat/world"}


class _ReportStats:
    def __init__(self):
        self.report = github_stats.StatsReport()

    @property
    async def name(self):
        return "Octocat"

    @property
    async def stargazers(self):
        return 1

    @property
    async def forks(self):
        return 2

    @property
    async def total_contributions(self):
        return 3

    @property
    async def current_year_contributions(self):
        return 4

    @property
    async def merged_pull_requests(self):
        return 5

    @property
    async def repos(self):
        return {"octocat/hello", "octocat/world"}

    @property
    async def views(self):
        return 6

    @property
    async def lines_changed(self):
        raise AssertionError("report should not read lines_changed")


class _MonthlyCacheStats:
    username = "octocat"

    def __init__(self):
        self.report = github_stats.StatsReport()
        self.scanned_window_keys = []

    @property
    async def repos(self):
        return {"octocat/hello"}

    async def scan_monthly_commits(
        self,
        windows,
        identity_patterns=None,
        extra_repos=None,
    ):
        self.scanned_window_keys = [window.key for window in windows]
        return github_stats.MonthlyCommitScanResult(
            counts={window.key: 7 for window in windows},
            repo_count=1,
            scanned_months=len(windows),
            identity_patterns=identity_patterns or [],
        )

    async def audit_monthly_commit_counts(self, windows):
        return {window.key: 3 for window in windows}


class _DegradedMonthlyCacheStats(_MonthlyCacheStats):
    async def scan_monthly_commits(
        self,
        windows,
        identity_patterns=None,
        extra_repos=None,
    ):
        self.scanned_window_keys = [window.key for window in windows]
        return github_stats.MonthlyCommitScanResult(
            counts={window.key: 0 for window in windows},
            repo_count=1,
            scanned_months=len(windows),
            identity_patterns=identity_patterns or [],
            degraded_months={window.key for window in windows},
        )


def _commit(
    sha,
    author_name="Other",
    author_email="other@example.com",
    author_date="2026-05-01T12:00:00Z",
    committer_name="Other",
    committer_email="other@example.com",
    committer_date="2026-05-01T12:00:00Z",
    author_login=None,
    committer_login=None,
):
    return {
        "sha": sha,
        "commit": {
            "author": {
                "name": author_name,
                "email": author_email,
                "date": author_date,
            },
            "committer": {
                "name": committer_name,
                "email": committer_email,
                "date": committer_date,
            },
        },
        "author": None if author_login is None else {"login": author_login},
        "committer": None if committer_login is None else {"login": committer_login},
    }


class GithubStatsTests(unittest.IsolatedAsyncioTestCase):
    def test_rolling_month_windows_include_current_plus_prior_12_months(self):
        # Arrange
        now = dt.datetime(2026, 5, 2, 10, 30, tzinfo=dt.timezone.utc)

        # Act
        windows = github_stats.rolling_month_windows(now)

        # Assert
        self.assertEqual(len(windows), 13)
        self.assertEqual(windows[0].key, "2025-05")
        self.assertEqual(windows[0].label, "May '25")
        self.assertEqual(windows[-1].key, "2026-05")
        self.assertTrue(windows[-1].is_current)
        self.assertEqual(windows[-1].until, "2026-05-02T10:30:00Z")

    def test_monthly_commit_identity_matching_checks_expected_fields(self):
        # Arrange
        patterns = ["Peter Ryszkiewicz", "prizz"]
        cases = [
            _commit("a", author_name="Peter Ryszkiewicz"),
            _commit("b", author_email="dev+prizz@example.com"),
            _commit("c", committer_name="pRizz"),
            _commit("d", author_login="pRizz"),
            _commit("e", committer_login="prizz"),
        ]
        non_match = _commit("f", author_name="Octo Cat")

        # Act
        matched = [
            github_stats._commit_identity_match_date(item, patterns)
            for item in cases
        ]

        # Assert
        self.assertTrue(all(item is not None for item in matched))
        self.assertIsNone(
            github_stats._commit_identity_match_date(non_match, patterns)
        )

    async def test_graphql_query_returns_json_for_success_status(self):
        # Arrange
        payload = {"data": {"viewer": {"login": "octocat"}}}
        queries = Queries("octocat", "token", _FailingSession())

        with mock.patch(
            "github_stats.requests.post",
            return_value=_FakeRequestsResponse(200, payload=payload),
        ):
            # Act
            result = await queries.query("query { viewer { login } }")

        # Assert
        self.assertEqual(result, payload)

    async def test_graphql_query_raises_after_repeated_502_responses(self):
        # Arrange
        queries = Queries("octocat", "token", _FailingSession())

        with mock.patch(
            "github_stats.requests.post", return_value=_FakeRequestsResponse()
        ), mock.patch("github_stats.asyncio.sleep", mock.AsyncMock()):
            # Act / Assert
            with self.assertRaises(github_stats.GitHubQueryError):
                await queries.query("query { viewer { login } }")

    async def test_graphql_query_raises_for_common_non_retryable_statuses(self):
        for status in (401, 403, 404, 422, 418):
            with self.subTest(status=status):
                # Arrange
                queries = Queries("octocat", "token", _FailingSession())

                with mock.patch(
                    "github_stats.requests.post",
                    return_value=_FakeRequestsResponse(
                        status, text=f"status {status}"
                    ),
                ):
                    # Act / Assert
                    with self.assertRaises(github_stats.GitHubQueryError):
                        await queries.query("query { viewer { login } }")

    async def test_graphql_query_retries_rate_limit_status_with_retry_after(self):
        # Arrange
        queries = Queries("octocat", "token", _FailingSession())

        with mock.patch(
            "github_stats.requests.post",
            return_value=_FakeRequestsResponse(
                429,
                text="secondary rate limit",
                headers={"Retry-After": "7"},
            ),
        ), mock.patch("github_stats.asyncio.sleep", mock.AsyncMock()) as sleep:
            # Act / Assert
            with self.assertRaises(github_stats.GitHubQueryError):
                await queries.query("query { viewer { login } }")

        sleep.assert_any_await(7.0)

    async def test_graphql_query_raises_for_non_json_success_response(self):
        # Arrange
        queries = Queries("octocat", "token", _FailingSession())

        with mock.patch(
            "github_stats.requests.post",
            return_value=_FakeRequestsResponse(200, text="not json"),
        ):
            # Act / Assert
            with self.assertRaises(github_stats.GitHubQueryError):
                await queries.query("query { viewer { login } }")

    async def test_get_stats_rejects_missing_required_graphql_data(self):
        # Arrange
        stats = Stats("octocat", "token", _FailingSession())
        stats.queries = mock.AsyncMock()
        stats.queries.query.return_value = {}

        # Act / Assert
        with self.assertRaises(github_stats.GitHubQueryError):
            await stats.get_stats()

        self.assertIsNone(stats._repos)

    async def test_current_year_contributions_uses_current_utc_year(self):
        # Arrange
        stats = Stats("octocat", "token", _FailingSession())
        stats.queries = mock.AsyncMock()
        stats.queries.query.return_value = {
            "data": {
                "viewer": {
                    "contributionsCollection": {
                        "contributionCalendar": {"totalContributions": 123}
                    }
                }
            }
        }

        # Act
        with mock.patch("github_stats._current_utc_year", return_value=2026):
            result = await stats.current_year_contributions

        # Assert
        self.assertEqual(result, 123)
        query = stats.queries.query.await_args.args[0]
        self.assertIn('from: "2026-01-01T00:00:00Z"', query)
        self.assertIn('to: "2027-01-01T00:00:00Z"', query)

    async def test_merged_pull_requests_reads_graphql_search_count(self):
        # Arrange
        stats = Stats("octocat", "token", _FailingSession())
        stats.queries = mock.AsyncMock()
        stats.queries.query.return_value = {
            "data": {"search": {"issueCount": 42}}
        }

        # Act
        result = await stats.merged_pull_requests

        # Assert
        self.assertEqual(result, 42)
        query = stats.queries.query.await_args.args[0]
        self.assertIn('query: "author:octocat is:pr is:merged"', query)
        self.assertIn("issueCount", query)

    async def test_monthly_commit_audit_reads_graphql_commit_contribution_counts(self):
        # Arrange
        windows = [
            github_stats.MonthWindow(
                key="2026-04",
                label="Apr '26",
                since="2026-04-01T00:00:00Z",
                until="2026-05-01T00:00:00Z",
                is_current=False,
            ),
            github_stats.MonthWindow(
                key="2026-05",
                label="May '26",
                since="2026-05-01T00:00:00Z",
                until="2026-05-02T00:00:00Z",
                is_current=True,
            ),
        ]
        stats = Stats("octocat", "token", _FailingSession())
        stats.queries = mock.AsyncMock()
        stats.queries.query.return_value = {
            "data": {
                "viewer": {
                    "m0": {"totalCommitContributions": 4},
                    "m1": {"totalCommitContributions": 5},
                }
            }
        }

        # Act
        result = await stats.audit_monthly_commit_counts(windows)

        # Assert
        self.assertEqual(result, {"2026-04": 4, "2026-05": 5})
        query = stats.queries.query.await_args.args[0]
        self.assertIn("totalCommitContributions", query)
        self.assertIn('from: "2026-04-01T00:00:00Z"', query)

    async def test_generate_images_stops_before_rendering_when_stats_load_fails(self):
        # Arrange
        async def fail_if_called(stats):
            raise AssertionError("rendering should not start")

        # Act / Assert
        with mock.patch.dict(
            "os.environ",
            {"ACCESS_TOKEN": "token", "GITHUB_ACTOR": "octocat"},
            clear=True,
        ), mock.patch("generate_images.Stats", _FailingStats), mock.patch(
            "generate_images.generate_languages", fail_if_called
        ), mock.patch(
            "generate_images.generate_overview", fail_if_called
        ):
            with self.assertRaises(RuntimeError):
                await generate_images.main()

    def test_generated_output_validation_rejects_corrupt_sentinel_values(self):
        # Arrange
        with TemporaryDirectory() as tmpdir:
            generated = Path(tmpdir) / "generated"
            generated.mkdir()
            (generated / "overview.svg").write_text(
                "No Name's GitHub Statistics\n"
                "Stars</td><td>0</td>\n"
                "Forks</td><td>0</td>\n"
                "Repositories with contributions</td><td>0</td>\n",
                encoding="utf-8",
            )
            (generated / "languages.svg").write_text(
                '<span class="progress">\n</span>\n<ul>\n\n</ul>\n',
                encoding="utf-8",
            )
            (generated / "monthly-commits.svg").write_text(
                'Monthly Commits\n<rect class="bar" />\n',
                encoding="utf-8",
            )

            # Act / Assert
            with self.assertRaises(RuntimeError):
                generate_images.validate_generated_output(str(generated))

    async def test_rest_query_returns_empty_dict_for_non_json_fallback(self):
        # Arrange
        queries = Queries("octocat", "token", _FailingSession())

        with mock.patch(
            "github_stats.requests.get", return_value=_FakeRequestsResponse(200)
        ):
            # Act
            result = await queries.query_rest("/repos/octocat/hello/traffic/views")

        # Assert
        self.assertEqual(result, {})

    async def test_rest_query_returns_empty_for_optional_not_found_statuses(self):
        for path, expected in (
            ("/repos/octocat/hello/stats/contributors", []),
            ("/repos/octocat/hello/traffic/views", {}),
            ("/repos/octocat/hello/commits", []),
        ):
            for status in (404, 410):
                with self.subTest(path=path, status=status):
                    # Arrange
                    queries = Queries("octocat", "token", _FailingSession())

                    with mock.patch(
                        "github_stats.requests.get",
                        return_value=_FakeRequestsResponse(
                            status, text=f"status {status}"
                        ),
                    ):
                        # Act
                        result = await queries.query_rest(path)

                    # Assert
                    self.assertEqual(result, expected)

    async def test_rest_query_returns_empty_for_unavailable_commit_listing(self):
        # Arrange
        queries = Queries("octocat", "token", _FailingSession())

        with mock.patch(
            "github_stats.requests.get",
            return_value=_FakeRequestsResponse(409, text="repository is empty"),
        ):
            # Act
            result = await queries.query_rest("/repos/octocat/hello/commits")

        # Assert
        self.assertEqual(result, [])

    async def test_rest_query_returns_empty_for_inaccessible_commit_listing(self):
        # Arrange
        queries = Queries("octocat", "token", _FailingSession())

        with mock.patch(
            "github_stats.requests.get",
            return_value=_FakeRequestsResponse(403, text="forbidden"),
        ):
            # Act
            result = await queries.query_rest("/repos/octocat/private/commits")

        # Assert
        self.assertEqual(result, [])

    async def test_rest_query_degrades_rate_limited_commit_listing(self):
        # Arrange
        queries = Queries("octocat", "token", _FailingSession())

        with mock.patch(
            "github_stats.requests.get",
            return_value=_FakeRequestsResponse(403, text="rate limit exceeded"),
        ):
            # Act
            result = await queries.query_rest(
                "/repos/octocat/private/commits",
                max_wait_seconds=0.0,
            )

        # Assert
        self.assertEqual(result, [])

    async def test_rest_query_returns_empty_for_inaccessible_traffic_views(self):
        # Arrange
        queries = Queries("octocat", "token", _FailingSession())

        with mock.patch(
            "github_stats.requests.get",
            return_value=_FakeRequestsResponse(
                403,
                text=(
                    '{"message":"Resource not accessible by personal access token",'
                    '"status":"403"}'
                ),
            ),
        ):
            # Act
            result = await queries.query_rest("/repos/octocat/hello/traffic/views")

        # Assert
        self.assertEqual(result, {})

    async def test_views_records_repo_for_inaccessible_traffic_endpoint(self):
        # Arrange
        stats = Stats("octocat", "token", _FailingSession())
        stats._repos = {"octocat/private"}
        stats.queries = _PathOutcomeQueries(
            {
                "/repos/octocat/private/traffic/views": github_stats.RestQueryResult(
                    {},
                    degraded=True,
                    status=403,
                    category="auth_or_permission_error",
                    message="traffic views inaccessible",
                    attempts=1,
                    retry_count=0,
                    wait_seconds=0.0,
                    elapsed_seconds=0.2,
                )
            }
        )

        # Act
        result = await stats.views

        # Assert
        self.assertEqual(result, 0)
        self.assertEqual(len(stats.report.traffic_degraded), 1)
        self.assertEqual(stats.report.traffic_degraded[0].repo, "octocat/private")
        self.assertEqual(stats.report.traffic_degraded[0].endpoint, "traffic/views")
        self.assertEqual(stats.report.rest_requests_total, 1)
        self.assertEqual(stats.report.slowest_requests[0].repo, "octocat/private")

    async def test_lines_changed_records_repo_for_contributor_degradation(self):
        # Arrange
        stats = Stats("octocat", "token", _FailingSession())
        stats._repos = {"octocat/slow"}
        stats.queries = _PathOutcomeQueries(
            {
                "/repos/octocat/slow/stats/contributors": github_stats.RestQueryResult(
                    [],
                    degraded=True,
                    status=202,
                    category="pending",
                    message="stats still pending",
                    attempts=3,
                    retry_count=2,
                    wait_seconds=4.3,
                    elapsed_seconds=4.5,
                )
            }
        )

        # Act
        result = await stats.lines_changed

        # Assert
        self.assertEqual(result, (0, 0))
        self.assertEqual(len(stats.report.contributors_degraded), 1)
        self.assertEqual(stats.report.contributors_degraded[0].repo, "octocat/slow")
        self.assertEqual(
            stats.report.contributors_degraded[0].endpoint,
            "stats/contributors",
        )
        self.assertEqual(stats.report.rest_requests_total, 1)
        self.assertEqual(stats.report.rest_retries_total, 2)
        self.assertEqual(stats.report.rest_wait_seconds_total, 4.3)
        self.assertEqual(stats.report.contributors_wait_seconds_total, 4.3)
        self.assertEqual(stats.report.contributors_degraded[0].wait_seconds, 4.3)

    async def test_rest_query_raises_for_auth_rate_limit_and_unknown_statuses(self):
        for status, headers in (
            (401, {}),
            (429, {"Retry-After": "5"}),
            (418, {}),
        ):
            with self.subTest(status=status):
                # Arrange
                queries = Queries("octocat", "token", _FailingSession())

                with mock.patch(
                    "github_stats.requests.get",
                    return_value=_FakeRequestsResponse(
                        status, text=f"status {status}", headers=headers
                    ),
                ), mock.patch("github_stats.asyncio.sleep", mock.AsyncMock()):
                    # Act / Assert
                    with self.assertRaises(github_stats.GitHubQueryError):
                        await queries.query_rest(
                            "/repos/octocat/hello/traffic/views",
                            max_wait_seconds=0.0,
                        )

    async def test_rest_query_returns_empty_value_for_success_without_body(self):
        # Arrange
        queries = Queries("octocat", "token", _FailingSession())

        with mock.patch(
            "github_stats.requests.get",
            return_value=_FakeRequestsResponse(204, text=""),
        ):
            # Act
            result = await queries.query_rest("/repos/octocat/hello/traffic/views")

        # Assert
        self.assertEqual(result, {})

    async def test_rest_query_can_skip_202_wait_for_contributor_stats(self):
        # Arrange
        queries = Queries("octocat", "token", _FailingSession())

        with mock.patch(
            "github_stats.requests.get", return_value=_Always202Response()
        ), mock.patch("github_stats.asyncio.sleep", mock.AsyncMock()) as sleep:
            # Act
            result = await queries.query_rest(
                "/repos/octocat/hello/stats/contributors",
                max_wait_seconds=0.0,
            )

        # Assert
        self.assertEqual(result, [])
        sleep.assert_not_awaited()

    async def test_rest_query_records_wait_stats_for_retryable_202(self):
        # Arrange
        queries = Queries("octocat", "token", _FailingSession())

        with mock.patch(
            "github_stats.requests.get", return_value=_Always202Response()
        ), mock.patch("github_stats.asyncio.sleep", mock.AsyncMock()) as sleep:
            # Act
            result = await queries.query_rest_with_outcome(
                "/repos/octocat/hello/stats/contributors",
                max_wait_seconds=1.0,
            )

        # Assert
        self.assertTrue(result.degraded)
        self.assertGreater(result.retry_count, 0)
        self.assertGreater(result.wait_seconds, 0)
        self.assertGreaterEqual(result.attempts, 1)
        sleep.assert_awaited()

    async def test_lines_changed_fetches_contributor_stats_concurrently(self):
        # Arrange
        stats = Stats("octocat", "token", _FailingSession())
        stats._repos = {"octocat/one", "octocat/two", "octocat/three"}
        queries = _ConcurrentContributorQueries()
        stats.queries = queries

        # Act
        result = await stats.lines_changed

        # Assert
        self.assertEqual(result, (30, 12))
        self.assertGreater(queries.max_active_requests, 1)

    def test_monthly_commit_throttle_env_defaults_and_overrides(self):
        # Act / Assert
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                github_stats._env_float(
                    "MONTHLY_COMMITS_REQUEST_DELAY_SECONDS",
                    0.75,
                ),
                0.75,
            )
            self.assertEqual(
                github_stats._env_int("MONTHLY_COMMITS_MAX_CONCURRENT_REQUESTS", 1),
                1,
            )

        with mock.patch.dict(
            "os.environ",
            {
                "MONTHLY_COMMITS_REQUEST_DELAY_SECONDS": "0.25",
                "MONTHLY_COMMITS_MAX_CONCURRENT_REQUESTS": "2",
            },
            clear=True,
        ):
            self.assertEqual(
                github_stats._env_float(
                    "MONTHLY_COMMITS_REQUEST_DELAY_SECONDS",
                    0.75,
                ),
                0.25,
            )
            self.assertEqual(
                github_stats._env_int("MONTHLY_COMMITS_MAX_CONCURRENT_REQUESTS", 1),
                2,
            )

    async def test_monthly_commit_scan_paginates_matches_and_dedupes_sha(self):
        # Arrange
        window = github_stats.MonthWindow(
            key="2026-05",
            label="May '26",
            since="2026-05-01T00:00:00Z",
            until="2026-05-02T00:00:00Z",
            is_current=True,
        )
        first_page = [
            _commit("match-1", author_name="Peter Ryszkiewicz"),
            *[
                _commit(f"other-{index}", author_name="Octocat")
                for index in range(99)
            ],
        ]
        second_page = [
            _commit("match-1", author_name="Peter Ryszkiewicz"),
            _commit("match-2", committer_login="pRizz"),
            _commit(
                "old",
                author_name="Peter Ryszkiewicz",
                author_date="2026-04-30T23:00:00Z",
            ),
        ]
        stats = Stats("octocat", "token", _FailingSession())
        stats._repos = {"octocat/hello"}
        stats.queries = _MonthlyCommitQueries(
            {
                ("/repos/octocat/hello/commits", "1"): github_stats.RestQueryResult(
                    first_page
                ),
                ("/repos/octocat/hello/commits", "2"): github_stats.RestQueryResult(
                    second_page
                ),
            }
        )

        # Act
        with mock.patch.dict(
            "os.environ",
            {"MONTHLY_COMMITS_REQUEST_DELAY_SECONDS": "0"},
        ):
            result = await stats.scan_monthly_commits(
                [window],
                identity_patterns=["Peter Ryszkiewicz", "prizz"],
            )

        # Assert
        self.assertEqual(result.counts, {"2026-05": 2})
        self.assertEqual(len(stats.queries.calls), 2)
        self.assertEqual(stats.queries.calls[0][1]["per_page"], "100")
        self.assertEqual(stats.report.rest_requests_total, 2)

    async def test_monthly_commit_scan_uses_global_request_delay(self):
        # Arrange
        window = github_stats.MonthWindow(
            key="2026-05",
            label="May '26",
            since="2026-05-01T00:00:00Z",
            until="2026-05-02T00:00:00Z",
            is_current=True,
        )
        stats = Stats("octocat", "token", _FailingSession())
        stats._repos = {"octocat/one", "octocat/two"}
        stats.queries = _MonthlyCommitQueries(
            {
                ("/repos/octocat/one/commits", "1"): github_stats.RestQueryResult([]),
                ("/repos/octocat/two/commits", "1"): github_stats.RestQueryResult([]),
            }
        )

        # Act
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch(
            "github_stats.asyncio.sleep",
            mock.AsyncMock(),
        ) as sleep:
            await stats.scan_monthly_commits([window])

        # Assert
        sleep.assert_awaited_once()
        self.assertGreaterEqual(sleep.await_args.args[0], 0.7)

    async def test_monthly_commit_scan_serializes_default_concurrency(self):
        # Arrange
        window = github_stats.MonthWindow(
            key="2026-05",
            label="May '26",
            since="2026-05-01T00:00:00Z",
            until="2026-05-02T00:00:00Z",
            is_current=True,
        )
        stats = Stats("octocat", "token", _FailingSession())
        stats._repos = {"octocat/one", "octocat/two", "octocat/three"}
        stats.queries = _SlowMonthlyCommitQueries(
            {
                ("/repos/octocat/one/commits", "1"): github_stats.RestQueryResult([]),
                ("/repos/octocat/two/commits", "1"): github_stats.RestQueryResult([]),
                ("/repos/octocat/three/commits", "1"): github_stats.RestQueryResult(
                    []
                ),
            }
        )

        # Act
        with mock.patch.dict(
            "os.environ",
            {"MONTHLY_COMMITS_REQUEST_DELAY_SECONDS": "0"},
            clear=True,
        ):
            await stats.scan_monthly_commits([window])

        # Assert
        self.assertEqual(stats.queries.max_active_requests, 1)

    async def test_monthly_commit_scan_records_degraded_repo_month(self):
        # Arrange
        window = github_stats.MonthWindow(
            key="2026-05",
            label="May '26",
            since="2026-05-01T00:00:00Z",
            until="2026-05-02T00:00:00Z",
            is_current=True,
        )
        stats = Stats("octocat", "token", _FailingSession())
        stats._repos = {"octocat/missing"}
        stats.queries = _MonthlyCommitQueries(
            {
                ("/repos/octocat/missing/commits", "1"): github_stats.RestQueryResult(
                    [],
                    degraded=True,
                    status=404,
                    category="not_found_or_gone",
                    message="missing",
                    attempts=1,
                    retry_count=0,
                    wait_seconds=0.0,
                    elapsed_seconds=0.1,
                )
            }
        )

        # Act
        with mock.patch.dict(
            "os.environ",
            {"MONTHLY_COMMITS_REQUEST_DELAY_SECONDS": "0"},
        ):
            result = await stats.scan_monthly_commits([window])

        # Assert
        self.assertEqual(result.counts, {"2026-05": 0})
        self.assertEqual(len(stats.report.monthly_commits_degraded), 1)
        self.assertEqual(stats.report.monthly_commits_degraded[0].repo, "octocat/missing")
        self.assertEqual(stats.report.monthly_commits_degraded[0].endpoint, "commits")

    async def test_generate_images_preloads_stats_before_concurrent_rendering(self):
        # Arrange
        _FakeStats.loaded = False
        _FakeStats.get_stats_calls = 0

        async def assert_loaded_before_render(stats):
            self.assertTrue(type(stats).loaded)

        # Act
        with mock.patch.dict(
            "os.environ",
            {"ACCESS_TOKEN": "token", "GITHUB_ACTOR": "octocat"},
            clear=True,
        ), mock.patch("generate_images.Stats", _FakeStats), mock.patch(
            "generate_images.generate_languages", assert_loaded_before_render
        ), mock.patch(
            "generate_images.generate_overview", assert_loaded_before_render
        ), mock.patch(
            "generate_images.generate_monthly_commits", assert_loaded_before_render
        ), mock.patch(
            "generate_images.validate_generated_output"
        ), mock.patch(
            "generate_images.build_run_report",
            mock.AsyncMock(
                return_value={
                    "stats": {
                        "name": "Octocat",
                        "stargazers": 0,
                        "forks": 0,
                        "total_contributions": 0,
                        "current_year_contributions": 0,
                        "merged_pull_requests": 0,
                        "repos": 0,
                        "views": 0,
                    },
                    "api": {
                        "total_repos": 0,
                        "contributors_degraded": [],
                        "traffic_degraded": [],
                        "monthly_commits_degraded": [],
                        "critical_failures": [],
                        "rest_requests_total": 0,
                        "rest_retries_total": 0,
                        "rest_wait_seconds_total": 0.0,
                        "contributors_wait_seconds_total": 0.0,
                        "traffic_wait_seconds_total": 0.0,
                        "monthly_commits_wait_seconds_total": 0.0,
                        "slowest_requests": [],
                    },
                    "monthly_commits": {
                        "total": 0,
                        "current_month": {},
                        "month_count": 0,
                    },
                }
            ),
        ), mock.patch(
            "generate_images.write_run_report"
        ), mock.patch(
            "generate_images.write_action_summary"
        ):
            await generate_images.main()

        # Assert
        self.assertEqual(_FakeStats.get_stats_calls, 1)

    async def test_generate_overview_uses_merged_prs_without_lines_changed(self):
        # Arrange
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "templates").mkdir()
            (tmp_path / "templates" / "overview.svg").write_text(
                "{{ name }} {{ stars }} {{ forks }} {{ contributions }} "
                "{{ merged_pull_requests }} {{ views }} {{ repos }}",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            _OverviewStats.lines_changed_accessed = False

            try:
                os.chdir(tmp_path)

                # Act
                await generate_images.generate_overview(_OverviewStats())

                # Assert
                output = (tmp_path / "generated" / "overview.svg").read_text(
                    encoding="utf-8"
                )
                self.assertIn("Octocat 1 2 3 42 4 2", output)
                self.assertFalse(_OverviewStats.lines_changed_accessed)
            finally:
                os.chdir(previous_cwd)

    async def test_monthly_commit_cache_refreshes_current_month_only(self):
        # Arrange
        now = dt.datetime(2026, 5, 2, 10, 30, tzinfo=dt.timezone.utc)
        windows = github_stats.rolling_month_windows(now)
        with TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "generated" / "monthly-commits.json"
            cache_path.parent.mkdir()
            cached_months = [
                {
                    "key": window.key,
                    "label": window.label,
                    "count": 1,
                    "is_current": window.is_current,
                }
                for window in windows[:-1]
            ]
            cache_path.write_text(
                generate_images.json.dumps({"months": cached_months}),
                encoding="utf-8",
            )
            stats = _MonthlyCacheStats()

            # Act
            result = await generate_images.build_monthly_commit_cache(
                stats,
                now=now,
                cache_path=str(cache_path),
            )

            # Assert
            self.assertEqual(stats.scanned_window_keys, ["2026-05"])
            self.assertEqual(result["months"][0]["count"], 1)
            self.assertEqual(result["months"][-1]["count"], 7)
            self.assertEqual(result["months"][-1]["github_attributed_count"], 3)

    async def test_monthly_commit_cache_preserves_count_when_refresh_degrades(self):
        # Arrange
        now = dt.datetime(2026, 5, 2, 10, 30, tzinfo=dt.timezone.utc)
        windows = github_stats.rolling_month_windows(now)
        with TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "generated" / "monthly-commits.json"
            cache_path.parent.mkdir()
            cache_path.write_text(
                generate_images.json.dumps(
                    {
                        "months": [
                            {
                                "key": window.key,
                                "label": window.label,
                                "count": 42 if window.is_current else 1,
                                "is_current": window.is_current,
                                "github_attributed_count": 5,
                            }
                            for window in windows
                        ]
                    }
                ),
                encoding="utf-8",
            )
            stats = _DegradedMonthlyCacheStats()

            # Act
            result = await generate_images.build_monthly_commit_cache(
                stats,
                now=now,
                cache_path=str(cache_path),
            )

            # Assert
            self.assertEqual(result["months"][-1]["count"], 42)
            self.assertTrue(result["months"][-1]["scan_degraded"])
            self.assertEqual(result["source"]["degraded_months"], 1)

    async def test_monthly_commit_cache_force_backfill_scans_all_months(self):
        # Arrange
        now = dt.datetime(2026, 5, 2, 10, 30, tzinfo=dt.timezone.utc)
        with TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "generated" / "monthly-commits.json"
            stats = _MonthlyCacheStats()

            # Act
            result = await generate_images.build_monthly_commit_cache(
                stats,
                force_backfill=True,
                now=now,
                cache_path=str(cache_path),
            )

            # Assert
            self.assertEqual(len(stats.scanned_window_keys), 13)
            self.assertEqual(result["source"]["scanned_months"], 13)
            self.assertEqual(
                result["source"]["audit_counting"],
                "graphql_total_commit_contributions",
            )
            self.assertTrue(cache_path.exists())

    def test_monthly_commit_bar_renderer_handles_zero_and_current_month(self):
        # Arrange
        months = [
            {"key": "2026-04", "label": "Apr '26", "count": 0, "is_current": False},
            {"key": "2026-05", "label": "May '26", "count": 12, "is_current": True},
        ]

        # Act
        output = generate_images._monthly_commit_bars(months)

        # Assert
        self.assertIn('class="bar"', output)
        self.assertIn('class="bar current"', output)
        self.assertIn("Apr &#x27;26", output)
        self.assertIn("12", output)

    async def test_build_run_report_includes_new_metrics_without_lines_changed(self):
        # Act
        report = await generate_images.build_run_report(_ReportStats())

        # Assert
        self.assertEqual(report["stats"]["current_year_contributions"], 4)
        self.assertEqual(report["stats"]["merged_pull_requests"], 5)
        self.assertNotIn("lines_changed", report["stats"])
        self.assertEqual(report["stats"]["views"], 6)

    def test_action_summary_includes_degraded_repo_names(self):
        # Arrange
        report = {
            "stats": {
                "name": "Octocat",
                "stargazers": 1,
                "forks": 2,
                "total_contributions": 3,
                "current_year_contributions": 4,
                "merged_pull_requests": 5,
                "repos": 4,
                "views": 0,
            },
            "api": {
                "total_repos": 4,
                "contributors_degraded": [
                    {
                        "repo": "octocat/slow",
                        "endpoint": "stats/contributors",
                        "status": 202,
                        "category": "pending",
                        "message": "stats still pending",
                        "attempts": 3,
                        "retry_count": 2,
                        "wait_seconds": 4.3,
                        "elapsed_seconds": 4.5,
                    }
                ],
                "traffic_degraded": [
                    {
                        "repo": "octocat/private",
                        "endpoint": "traffic/views",
                        "status": 403,
                        "category": "auth_or_permission_error",
                        "message": "traffic views inaccessible",
                        "attempts": 1,
                        "retry_count": 0,
                        "wait_seconds": 0.0,
                        "elapsed_seconds": 0.2,
                    }
                ],
                "critical_failures": [],
                "rest_requests_total": 2,
                "rest_retries_total": 2,
                "rest_wait_seconds_total": 4.3,
                "contributors_wait_seconds_total": 4.3,
                "traffic_wait_seconds_total": 0.0,
                "slowest_requests": [
                    {
                        "repo": "octocat/slow",
                        "endpoint": "stats/contributors",
                        "status": 202,
                        "category": "pending",
                        "degraded": True,
                        "attempts": 3,
                        "retry_count": 2,
                        "wait_seconds": 4.3,
                        "elapsed_seconds": 4.5,
                        "message": "stats still pending",
                    }
                ],
            },
        }

        # Act
        summary = generate_images.render_action_summary(report)

        # Assert
        self.assertIn("octocat/private", summary)
        self.assertIn("octocat/slow", summary)
        self.assertIn("Traffic views degraded: 1", summary)
        self.assertIn("Contributor stats degraded: 1", summary)
        self.assertIn("Current-year contributions", summary)
        self.assertIn("Merged pull requests", summary)
        self.assertIn("API Wait Summary", summary)
        self.assertIn("Slowest API Waits", summary)
        self.assertIn("4.3s", summary)

    def test_run_report_validation_allows_contributor_degradation(self):
        # Arrange
        report = {
            "stats": {
                "name": "Octocat",
                "stargazers": 1,
                "forks": 2,
                "total_contributions": 3,
                "current_year_contributions": 4,
                "merged_pull_requests": 5,
                "repos": 4,
                "views": 7,
            },
            "api": {
                "contributors_degraded": [
                    {
                        "repo": "octocat/slow",
                        "endpoint": "stats/contributors",
                        "status": 202,
                        "category": "pending",
                        "message": "stats still pending",
                    }
                ],
                "traffic_degraded": [],
            },
        }

        # Act / Assert
        generate_images.validate_run_report(report)

    def test_run_report_validation_allows_zero_views_with_degraded_traffic_by_default(
        self,
    ):
        # Arrange
        report = {
            "stats": {
                "name": "Octocat",
                "stargazers": 1,
                "forks": 2,
                "total_contributions": 3,
                "current_year_contributions": 4,
                "merged_pull_requests": 5,
                "repos": 4,
                "views": 0,
            },
            "api": {
                "contributors_degraded": [],
                "traffic_degraded": [
                    {
                        "repo": "octocat/private",
                        "endpoint": "traffic/views",
                        "status": 403,
                        "category": "auth_or_permission_error",
                        "message": "traffic views inaccessible",
                    }
                ],
            },
        }

        # Act / Assert
        with mock.patch.dict("os.environ", {}, clear=True):
            generate_images.validate_run_report(report)

    def test_run_report_validation_strict_mode_rejects_degraded_traffic(self):
        # Arrange
        report = {
            "stats": {
                "name": "Octocat",
                "stargazers": 1,
                "forks": 2,
                "total_contributions": 3,
                "current_year_contributions": 4,
                "merged_pull_requests": 5,
                "repos": 4,
                "views": 0,
            },
            "api": {
                "contributors_degraded": [],
                "traffic_degraded": [
                    {
                        "repo": "octocat/private",
                        "endpoint": "traffic/views",
                        "status": 403,
                        "category": "auth_or_permission_error",
                        "message": "traffic views inaccessible",
                    }
                ],
            },
        }

        # Act / Assert
        with mock.patch.dict(
            "os.environ",
            {"STRICT_TRAFFIC_VIEW_VALIDATION": "true"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "Repository views"):
                generate_images.validate_run_report(report)

    def test_run_report_validation_allows_real_zero_without_degradation(self):
        # Arrange
        report = {
            "stats": {
                "name": "Octocat",
                "stargazers": 0,
                "forks": 0,
                "total_contributions": 0,
                "current_year_contributions": 0,
                "merged_pull_requests": 0,
                "repos": 1,
                "views": 0,
            },
            "api": {
                "contributors_degraded": [],
                "traffic_degraded": [],
            },
        }

        # Act / Assert
        generate_images.validate_run_report(report)

    def test_env_truthy_parses_common_values(self):
        # Act / Assert
        for value in ("true", "1", "yes", "on", "anything"):
            with self.subTest(value=value), mock.patch.dict(
                "os.environ",
                {"STRICT_TRAFFIC_VIEW_VALIDATION": value},
                clear=True,
            ):
                self.assertTrue(
                    generate_images.env_truthy("STRICT_TRAFFIC_VIEW_VALIDATION")
                )

        for value in ("", "false", "0", "no", "off"):
            with self.subTest(value=value), mock.patch.dict(
                "os.environ",
                {"STRICT_TRAFFIC_VIEW_VALIDATION": value},
                clear=True,
            ):
                self.assertFalse(
                    generate_images.env_truthy("STRICT_TRAFFIC_VIEW_VALIDATION")
                )

    def test_action_summary_writer_allows_missing_github_step_summary(self):
        # Act / Assert
        with mock.patch.dict("os.environ", {}, clear=True):
            generate_images.write_action_summary("# Summary")


if __name__ == "__main__":
    unittest.main()
