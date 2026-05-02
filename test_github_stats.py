#!/usr/bin/python3

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


class GithubStatsTests(unittest.IsolatedAsyncioTestCase):
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
                        "critical_failures": [],
                        "rest_requests_total": 0,
                        "rest_retries_total": 0,
                        "rest_wait_seconds_total": 0.0,
                        "contributors_wait_seconds_total": 0.0,
                        "traffic_wait_seconds_total": 0.0,
                        "slowest_requests": [],
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

    def test_run_report_validation_rejects_zero_views_with_degraded_traffic(self):
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

    def test_action_summary_writer_allows_missing_github_step_summary(self):
        # Act / Assert
        with mock.patch.dict("os.environ", {}, clear=True):
            generate_images.write_action_summary("# Summary")


if __name__ == "__main__":
    unittest.main()
