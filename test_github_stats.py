#!/usr/bin/python3

import asyncio
import datetime as dt
import shutil
import unittest
import os
import re
import typing
from xml.etree import ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import requests

import github_stats
import generate_images
import language_icons
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
    init_args = None

    def __init__(self, *args, **kwargs):
        type(self).init_args = (args, kwargs)

    async def get_stats(self):
        type(self).loaded = True
        type(self).get_stats_calls += 1

    @property
    async def stargazers(self):
        return 17

    @property
    async def repos(self):
        return {"octocat/hello", "octocat/world"}

    @property
    async def repo_metrics(self):
        return {
            "octocat/hello": github_stats.RepoMetric(
                name_with_owner="octocat/hello",
                display_name="octocat/hello",
                is_owned_by_viewer=True,
                is_private=False,
                is_fork=False,
                is_archived=False,
                stargazers=10,
                forks=1,
                updated_at="2026-06-01T00:00:00Z",
                pushed_at="2026-06-01T00:00:00Z",
                open_issues=0,
                open_pull_requests=0,
                releases=0,
                tags=0,
                languages={},
            ),
            "octocat/world": github_stats.RepoMetric(
                name_with_owner="octocat/world",
                display_name="octocat/world",
                is_owned_by_viewer=True,
                is_private=False,
                is_fork=False,
                is_archived=False,
                stargazers=7,
                forks=1,
                updated_at="2026-06-01T00:00:00Z",
                pushed_at="2026-06-01T00:00:00Z",
                open_issues=0,
                open_pull_requests=0,
                releases=0,
                tags=0,
                languages={},
            ),
        }


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


class _ExperimentalStats:
    username = "octocat"

    def __init__(self):
        self.report = github_stats.StatsReport()

    @property
    async def repo_metrics(self):
        return {
            "octocat/public": github_stats.RepoMetric(
                name_with_owner="octocat/public",
                display_name="octocat/public",
                is_owned_by_viewer=True,
                is_private=False,
                is_fork=False,
                is_archived=False,
                stargazers=8,
                forks=3,
                updated_at="2026-05-01T00:00:00Z",
                pushed_at="2026-05-01T00:00:00Z",
                open_issues=2,
                open_pull_requests=1,
                releases=1,
                tags=2,
                languages={
                    "Python": {"size": 100, "color": "#3572A5"},
                    "Shell": {"size": 20, "color": "#89e051"},
                },
            ),
            "octocat/secret": github_stats.RepoMetric(
                name_with_owner="octocat/secret",
                display_name="Private repo #1",
                is_owned_by_viewer=True,
                is_private=True,
                is_fork=True,
                is_archived=False,
                stargazers=5,
                forks=1,
                updated_at="2025-01-01T00:00:00Z",
                pushed_at="2025-01-01T00:00:00Z",
                open_issues=4,
                open_pull_requests=2,
                releases=0,
                tags=0,
                languages={"Rust": {"size": 50, "color": "#dea584"}},
            ),
            "example/huge": github_stats.RepoMetric(
                name_with_owner="example/huge",
                display_name="example/huge",
                is_owned_by_viewer=False,
                is_private=False,
                is_fork=False,
                is_archived=False,
                stargazers=10_000,
                forks=2_000,
                updated_at="2026-05-01T00:00:00Z",
                pushed_at="2026-05-01T00:00:00Z",
                open_issues=0,
                open_pull_requests=0,
                releases=0,
                tags=0,
                languages={"Python": {"size": 10, "color": "#3572A5"}},
            ),
        }

    @property
    async def stargazers(self):
        return 8

    @property
    async def forks(self):
        return 3

    @property
    async def merged_pull_requests(self):
        return 7

    @property
    async def current_year_contribution_breakdown(self):
        return github_stats.ContributionBreakdown(
            commits=11,
            issues=2,
            pull_requests=3,
            pull_request_reviews=4,
            repositories=1,
            restricted=1,
        )

    @property
    async def lines_changed(self):
        raise AssertionError("experimental metrics should not read lines_changed")

    @property
    async def views(self):
        return 25

    @property
    async def clones(self):
        return 6

    async def scan_commit_windows(
        self,
        windows,
        identity_patterns=None,
        extra_repos=None,
    ):
        return github_stats.CommitWindowScanResult(
            counts={
                window.key: {
                    "trailing_30_days": 72,
                    "trailing_180_days": 360,
                    "trailing_365_days": 365,
                }.get(window.key, 0)
                for window in windows
            },
            repo_count=2,
            scanned_windows=len(windows),
            identity_patterns=identity_patterns or [],
        )


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


def _repo_node(name, is_fork=False, stargazers=0, forks=0):
    return {
        "nameWithOwner": name,
        "isPrivate": False,
        "isFork": is_fork,
        "isArchived": False,
        "updatedAt": "2026-06-01T00:00:00Z",
        "pushedAt": "2026-06-01T00:00:00Z",
        "stargazers": {"totalCount": stargazers},
        "forkCount": forks,
        "issues": {"totalCount": 0},
        "pullRequests": {"totalCount": 0},
        "refs": {"totalCount": 0},
        "languages": {"edges": []},
    }


def _owned_repo_response(nodes):
    return {
        "data": {
            "viewer": {
                "login": "octocat",
                "name": "Octocat",
                "repositories": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": nodes,
                },
            }
        }
    }


def _contributed_repo_response(nodes):
    return {
        "data": {
            "viewer": {
                "login": "octocat",
                "name": "Octocat",
                "repositoriesContributedTo": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": nodes,
                },
            }
        }
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
                "Owned stars</td><td>0</td>\n"
                "Owned forks</td><td>0</td>\n"
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

    def test_repository_overview_queries_use_bounded_page_size(self):
        # Arrange
        expected = f"first: {github_stats.Queries.REPOSITORY_OVERVIEW_PAGE_SIZE}"

        # Act
        owned_query = github_stats.Queries.owned_repos_overview()
        contributed_query = github_stats.Queries.contributed_repos_overview()

        # Assert
        self.assertIn(expected, owned_query)
        self.assertIn(expected, contributed_query)
        self.assertNotIn("first: 100", owned_query)
        self.assertNotIn("first: 100", contributed_query)

    def test_owned_repository_query_can_include_forks(self):
        # Arrange / Act
        default_query = github_stats.Queries.owned_repos_overview()
        include_forks_query = github_stats.Queries.owned_repos_overview(
            include_forks=True
        )

        # Assert
        self.assertIn("isFork: false", default_query)
        self.assertNotIn("isFork: false", include_forks_query)

    async def test_get_stats_includes_non_fork_contributed_repos_when_ignoring_forks(
        self,
    ):
        # Arrange
        stats = Stats(
            "octocat",
            "token",
            _FailingSession(),
            ignore_forked_repos=True,
        )
        stats.queries = mock.AsyncMock()
        stats.queries.query.side_effect = [
            _owned_repo_response(
                [
                    _repo_node("octocat/owned", stargazers=10, forks=2),
                    _repo_node(
                        "octocat/owned-fork",
                        is_fork=True,
                        stargazers=500,
                        forks=50,
                    ),
                ]
            ),
            _contributed_repo_response(
                [
                    _repo_node("example/contributed", stargazers=10_000, forks=1_000),
                    _repo_node(
                        "example/contributed-fork",
                        is_fork=True,
                        stargazers=20_000,
                        forks=2_000,
                    ),
                ]
            ),
        ]

        # Act
        await stats.get_stats()

        # Assert
        self.assertEqual(
            await stats.repos,
            {"octocat/owned", "example/contributed"},
        )
        self.assertEqual(await stats.stargazers, 10)
        self.assertEqual(await stats.forks, 2)
        repo_metrics = await stats.repo_metrics
        self.assertTrue(repo_metrics["octocat/owned"].is_owned_by_viewer)
        self.assertFalse(repo_metrics["example/contributed"].is_owned_by_viewer)
        self.assertEqual(stats.queries.query.await_count, 2)
        owned_query = stats.queries.query.await_args_list[0].args[0]
        contributed_query = stats.queries.query.await_args_list[1].args[0]
        self.assertIn("isFork: false", owned_query)
        self.assertIn("repositoriesContributedTo", contributed_query)

    async def test_get_stats_includes_owned_and_contributed_forks_when_enabled(self):
        # Arrange
        stats = Stats(
            "octocat",
            "token",
            _FailingSession(),
            ignore_forked_repos=False,
        )
        stats.queries = mock.AsyncMock()
        stats.queries.query.side_effect = [
            _owned_repo_response(
                [
                    _repo_node("octocat/owned", stargazers=10, forks=2),
                    _repo_node(
                        "octocat/owned-fork",
                        is_fork=True,
                        stargazers=500,
                        forks=50,
                    ),
                ]
            ),
            _contributed_repo_response(
                [
                    _repo_node("example/contributed", stargazers=10_000, forks=1_000),
                    _repo_node(
                        "example/contributed-fork",
                        is_fork=True,
                        stargazers=20_000,
                        forks=2_000,
                    ),
                ]
            ),
        ]

        # Act
        await stats.get_stats()

        # Assert
        self.assertEqual(
            await stats.repos,
            {
                "octocat/owned",
                "octocat/owned-fork",
                "example/contributed",
                "example/contributed-fork",
            },
        )
        self.assertEqual(await stats.stargazers, 10)
        self.assertEqual(await stats.forks, 2)
        repo_metrics = await stats.repo_metrics
        self.assertTrue(repo_metrics["octocat/owned"].is_owned_by_viewer)
        self.assertTrue(repo_metrics["octocat/owned-fork"].is_owned_by_viewer)
        self.assertFalse(repo_metrics["example/contributed"].is_owned_by_viewer)
        self.assertFalse(repo_metrics["example/contributed-fork"].is_owned_by_viewer)
        owned_query = stats.queries.query.await_args_list[0].args[0]
        self.assertNotIn("isFork: false", owned_query)

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

    async def test_monthly_commit_scan_dedupes_sha_across_repositories(self):
        # Arrange
        window = github_stats.MonthWindow(
            key="2026-05",
            label="May '26",
            since="2026-05-01T00:00:00Z",
            until="2026-05-02T00:00:00Z",
            is_current=True,
        )
        stats = Stats("octocat", "token", _FailingSession())
        stats._repos = {"octocat/upstream", "octocat/fork"}
        stats.queries = _MonthlyCommitQueries(
            {
                (
                    "/repos/octocat/fork/commits",
                    "1",
                ): github_stats.RestQueryResult(
                    [_commit("same-sha", author_name="Peter Ryszkiewicz")]
                ),
                (
                    "/repos/octocat/upstream/commits",
                    "1",
                ): github_stats.RestQueryResult(
                    [_commit("same-sha", author_name="Peter Ryszkiewicz")]
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
                identity_patterns=["Peter Ryszkiewicz"],
            )

        # Assert
        self.assertEqual(result.counts, {"2026-05": 1})

    async def test_commit_window_scan_counts_arbitrary_trailing_windows(self):
        # Arrange
        windows = [
            github_stats.CommitWindow(
                key="trailing_30_days",
                label="30 days",
                since="2026-04-02T00:00:00Z",
                until="2026-05-02T00:00:00Z",
            ),
            github_stats.CommitWindow(
                key="trailing_365_days",
                label="365 days",
                since="2025-05-02T00:00:00Z",
                until="2026-05-02T00:00:00Z",
            ),
        ]
        stats = Stats("octocat", "token", _FailingSession())
        stats._repos = {"octocat/hello"}
        stats.queries = _MonthlyCommitQueries(
            {
                ("/repos/octocat/hello/commits", "1"): github_stats.RestQueryResult(
                    [
                        _commit(
                            "recent",
                            author_name="Peter Ryszkiewicz",
                            author_date="2026-05-01T12:00:00Z",
                        ),
                        _commit(
                            "boundary-start",
                            author_name="Peter Ryszkiewicz",
                            author_date="2026-04-02T00:00:00Z",
                        ),
                        _commit(
                            "year-only",
                            author_name="Peter Ryszkiewicz",
                            author_date="2025-06-01T12:00:00Z",
                        ),
                        _commit(
                            "too-old",
                            author_name="Peter Ryszkiewicz",
                            author_date="2025-05-01T23:59:59Z",
                        ),
                        _commit(
                            "until-boundary",
                            author_name="Peter Ryszkiewicz",
                            author_date="2026-05-02T00:00:00Z",
                        ),
                        _commit("other", author_name="Octocat"),
                    ]
                ),
            }
        )

        # Act
        with mock.patch.dict(
            "os.environ",
            {"MONTHLY_COMMITS_REQUEST_DELAY_SECONDS": "0"},
        ):
            result = await stats.scan_commit_windows(
                windows,
                identity_patterns=["Peter Ryszkiewicz"],
            )

        # Assert
        self.assertEqual(
            result.counts,
            {
                "trailing_30_days": 2,
                "trailing_365_days": 3,
            },
        )
        self.assertEqual(result.scanned_windows, 2)
        self.assertEqual(len(stats.queries.calls), 1)
        self.assertEqual(
            stats.queries.calls[0][1]["since"],
            "2025-05-02T00:00:00Z",
        )
        self.assertEqual(
            stats.queries.calls[0][1]["until"],
            "2026-05-02T00:00:00Z",
        )

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
        _FakeStats.init_args = None

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
            "generate_images.generate_experimental", assert_loaded_before_render
        ), mock.patch(
            "generate_images.record_star_history_sample"
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
                        "traffic_degraded": [],
                        "monthly_commits_degraded": [],
                        "critical_failures": [],
                        "rest_requests_total": 0,
                        "rest_retries_total": 0,
                        "rest_wait_seconds_total": 0.0,
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

    def test_star_history_cache_writes_header_upserts_and_sorts(self):
        # Arrange
        with TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "generated" / "star-history.csv"

            # Act
            generate_images.record_star_history_sample(
                10,
                2,
                now=dt.datetime(2026, 5, 3, 10, 0, tzinfo=dt.timezone.utc),
                path=str(cache_path),
            )
            generate_images.record_star_history_sample(
                8,
                1,
                now=dt.datetime(2026, 5, 1, 10, 0, tzinfo=dt.timezone.utc),
                path=str(cache_path),
            )
            result = generate_images.record_star_history_sample(
                13,
                2,
                now=dt.datetime(2026, 5, 3, 11, 0, tzinfo=dt.timezone.utc),
                path=str(cache_path),
            )
            records = generate_images.load_star_history_cache(str(cache_path))

            # Assert
            self.assertEqual(result["samples"], 2)
            self.assertEqual([record["date"] for record in records], ["2026-05-01", "2026-05-03"])
            self.assertEqual(records[-1]["total_stargazers"], 13)
            self.assertEqual(records[-1]["repo_count"], 2)
            self.assertEqual(records[-1]["captured_at"], "2026-05-03T11:00:00Z")
            self.assertEqual(
                cache_path.read_text(encoding="utf-8").splitlines()[0],
                "date,total_stargazers,repo_count,captured_at",
            )
            self.assertEqual(set(records[0].keys()), set(generate_images.STAR_HISTORY_FIELDNAMES))

    def test_star_history_metrics_bucket_sparse_windows(self):
        # Arrange
        now = dt.datetime(2026, 6, 5, 10, 30, tzinfo=dt.timezone.utc)
        with TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "generated" / "star-history.csv"
            generate_images.write_star_history_cache(
                [
                    {
                        "date": "2025-06-06",
                        "total_stargazers": 70,
                        "repo_count": 2,
                        "captured_at": "2025-06-06T01:00:00Z",
                    },
                    {
                        "date": "2025-06-07",
                        "total_stargazers": 71,
                        "repo_count": 2,
                        "captured_at": "2025-06-07T01:00:00Z",
                    },
                    {
                        "date": "2026-03-08",
                        "total_stargazers": 90,
                        "repo_count": 2,
                        "captured_at": "2026-03-08T01:00:00Z",
                    },
                    {
                        "date": "2026-03-10",
                        "total_stargazers": 95,
                        "repo_count": 2,
                        "captured_at": "2026-03-10T01:00:00Z",
                    },
                    {
                        "date": "2026-05-07",
                        "total_stargazers": 120,
                        "repo_count": 2,
                        "captured_at": "2026-05-07T01:00:00Z",
                    },
                    {
                        "date": "2026-06-05",
                        "total_stargazers": 130,
                        "repo_count": 2,
                        "captured_at": "2026-06-05T01:00:00Z",
                    },
                ],
                path=str(cache_path),
            )

            # Act
            metrics = generate_images.build_star_history_metrics(
                now=now,
                cache_path=str(cache_path),
            )
            windows = {window["key"]: window for window in metrics["windows"]}

            # Assert
            self.assertEqual(windows["30_days"]["bucket_days"], 1)
            self.assertEqual(windows["30_days"]["delta"], 10)
            self.assertEqual(
                [point["date"] for point in windows["30_days"]["points"]],
                ["2026-05-07", "2026-06-05"],
            )
            self.assertEqual(windows["90_days"]["bucket_days"], 3)
            self.assertEqual(windows["90_days"]["points"][0]["date"], "2026-03-10")
            self.assertEqual(windows["90_days"]["raw_sample_count"], 4)
            self.assertEqual(windows["365_days"]["bucket_days"], 7)
            self.assertEqual(windows["365_days"]["points"][0]["date"], "2025-06-07")
            self.assertEqual(windows["365_days"]["latest_total"], 130)

    def test_star_history_chart_handles_single_sample_inside_card(self):
        # Arrange
        window = {
            "days": 30,
            "latest_total": 42,
            "delta": 0,
            "sample_count": 1,
            "points": [
                {
                    "date": "2026-06-05",
                    "offset_days": 29,
                    "total_stargazers": 42,
                    "repo_count": 2,
                }
            ],
        }

        # Act
        output = generate_images._star_history_chart(window)
        coordinates = [
            float(value)
            for value in re.findall(r'(?:cx|cy)="([0-9.]+)"', output)
        ]
        endpoint_labels = re.findall(
            r'<text class="endpoint-label" x="([0-9.]+)" y="([0-9.]+)" '
            r'text-anchor="([^"]+)">([^<]+)</text>',
            output,
        )

        # Assert
        self.assertIn("<circle", output)
        self.assertNotIn('<path class="chart-line"', output)
        self.assertGreaterEqual(coordinates[0], 24)
        self.assertLessEqual(coordinates[0], 336)
        self.assertGreaterEqual(coordinates[1], 78)
        self.assertLessEqual(coordinates[1], 156)
        self.assertEqual(len(endpoint_labels), 1)
        self.assertEqual(endpoint_labels[0][3], "42")

    def test_star_history_chart_labels_first_and_last_points(self):
        # Arrange
        window = {
            "days": 30,
            "latest_total": 1_250,
            "delta": 250,
            "sample_count": 3,
            "points": [
                {
                    "date": "2026-05-07",
                    "offset_days": 0,
                    "total_stargazers": 1_000,
                    "repo_count": 2,
                },
                {
                    "date": "2026-05-20",
                    "offset_days": 13,
                    "total_stargazers": 1_125,
                    "repo_count": 2,
                },
                {
                    "date": "2026-06-05",
                    "offset_days": 29,
                    "total_stargazers": 1_250,
                    "repo_count": 2,
                },
            ],
        }

        # Act
        output = generate_images._star_history_chart(window)
        endpoint_labels = re.findall(
            r'<text class="endpoint-label" x="([0-9.]+)" y="([0-9.]+)" '
            r'text-anchor="([^"]+)">([^<]+)</text>',
            output,
        )

        # Assert
        self.assertEqual(len(endpoint_labels), 2)
        self.assertEqual([label[3] for label in endpoint_labels], ["1,000", "1,250"])
        self.assertEqual([label[2] for label in endpoint_labels], ["start", "end"])
        for x, y, _anchor, _value in endpoint_labels:
            self.assertGreaterEqual(float(x), 21)
            self.assertLessEqual(float(x), 339)
            self.assertGreaterEqual(float(y), 68)
            self.assertLessEqual(float(y), 148)

    async def test_commit_velocity_metrics_compute_trailing_rates(self):
        # Arrange
        now = dt.datetime(2026, 5, 2, 10, 30, tzinfo=dt.timezone.utc)

        # Act
        metrics = await generate_images.build_commit_velocity_metrics(
            _ExperimentalStats(),
            now=now,
        )

        # Assert
        self.assertEqual(metrics["source"]["counting"], "rest_commits_identity_scan")
        self.assertEqual(metrics["source"]["timezone"], "UTC")
        self.assertEqual(metrics["source"]["repo_count"], 2)
        self.assertEqual(
            [item["key"] for item in metrics["windows"]],
            [
                "trailing_30_days",
                "trailing_180_days",
                "trailing_365_days",
            ],
        )
        self.assertEqual(metrics["windows"][0]["key"], "trailing_30_days")
        self.assertEqual(metrics["windows"][0]["commits"], 72)
        self.assertEqual(metrics["windows"][0]["per_hour"], 0.1)
        self.assertEqual(metrics["windows"][0]["per_day"], 2.4)
        self.assertEqual(metrics["windows"][0]["per_week"], 16.8)
        self.assertEqual(metrics["windows"][0]["per_month"], 72.0)
        self.assertEqual(metrics["windows"][1]["key"], "trailing_180_days")
        self.assertEqual(metrics["windows"][1]["label"], "6 months")
        self.assertEqual(metrics["windows"][1]["days"], 180)
        self.assertEqual(metrics["windows"][1]["commits"], 360)
        self.assertEqual(metrics["windows"][1]["per_month"], 60.0)
        self.assertEqual(metrics["windows"][2]["key"], "trailing_365_days")
        self.assertEqual(metrics["windows"][2]["commits"], 365)
        self.assertEqual(metrics["windows"][2]["per_day"], 1.0)
        self.assertEqual(metrics["windows"][2]["per_month"], 30.0)

    async def test_experimental_metrics_redact_private_repository_names(self):
        # Arrange
        now = dt.datetime(2026, 5, 2, 10, 30, tzinfo=dt.timezone.utc)
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "generated").mkdir()
            (tmp_path / "generated" / "monthly-commits.json").write_text(
                generate_images.json.dumps(
                    {
                        "months": [
                            {
                                "key": "2026-04",
                                "label": "Apr '26",
                                "count": 5,
                                "is_current": False,
                            },
                            {
                                "key": "2026-05",
                                "label": "May '26",
                                "count": 7,
                                "is_current": True,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()

            try:
                os.chdir(tmp_path)

                # Act
                metrics = await generate_images.build_experimental_metrics(
                    _ExperimentalStats(),
                    now=now,
                )

                # Assert
                serialized = generate_images.json.dumps(metrics)
                self.assertIn("Private repo #1", serialized)
                self.assertNotIn("octocat/secret", serialized)
                self.assertIn("octocat/public", serialized)
                self.assertIn("example/huge", serialized)
                self.assertEqual(metrics["privacy"]["private_repositories"], 1)
                self.assertEqual(metrics["impact"]["stars"], 8)
                self.assertEqual(metrics["impact"]["forks"], 3)
                self.assertEqual(
                    metrics["impact"]["top_public_repo"]["display_name"],
                    "octocat/public",
                )
                self.assertEqual(
                    metrics["personal_bests"]["top_public_repo"],
                    "octocat/public",
                )
                self.assertEqual(
                    [
                        repo["display_name"]
                        for repo in metrics["repo_portfolio"]["repositories"]
                    ],
                    ["octocat/public"],
                )
                self.assertEqual(
                    metrics["repo_portfolio"]["public_repositories"],
                    1,
                )
                self.assertEqual(
                    metrics["repo_portfolio"]["private_repositories"],
                    0,
                )
                self.assertEqual(
                    metrics["collaboration"]["owned_repositories"],
                    2,
                )
                self.assertEqual(
                    metrics["collaboration"]["external_repositories"],
                    1,
                )
                self.assertNotIn("code_footprint", metrics)
                self.assertIn("commit_velocity", metrics)
                self.assertEqual(
                    metrics["commit_velocity"]["windows"][0]["label"],
                    "30 days",
                )
            finally:
                os.chdir(previous_cwd)

    def test_experimental_card_names_exclude_code_footprint(self):
        # Assert
        self.assertNotIn("code-footprint", generate_images.EXPERIMENTAL_CARD_NAMES)
        self.assertIn("commit-velocity", generate_images.EXPERIMENTAL_CARD_NAMES)
        self.assertIn("star-history-30-days", generate_images.EXPERIMENTAL_CARD_NAMES)
        self.assertIn("star-history-90-days", generate_images.EXPERIMENTAL_CARD_NAMES)
        self.assertIn("star-history-365-days", generate_images.EXPERIMENTAL_CARD_NAMES)

    def test_readme_lists_commit_velocity_and_star_history_cards(self):
        # Arrange
        readme = Path("README.md").read_text(encoding="utf-8")

        # Assert
        self.assertIn("generated/experimental-commit-velocity.svg", readme)
        self.assertIn("generated/experimental-star-history-30-days.svg", readme)
        self.assertIn("generated/experimental-star-history-90-days.svg", readme)
        self.assertIn("generated/experimental-star-history-365-days.svg", readme)

    def test_experimental_renderer_type_hints_resolve(self):
        # Act / Assert
        typing.get_type_hints(generate_images._experimental_rows)
        typing.get_type_hints(generate_images._experimental_horizontal_bars)
        typing.get_type_hints(generate_images._experimental_stack_bar)
        typing.get_type_hints(generate_images._commit_velocity_table)
        typing.get_type_hints(generate_images._star_history_chart)

    def test_commit_velocity_table_aligns_columns(self):
        # Arrange
        windows = [
            {
                "label": "30 days",
                "commits": 360,
                "per_hour": 0.5,
                "per_day": 12.0,
                "per_week": 84.0,
                "per_month": 360.0,
            },
            {
                "label": "6 months",
                "commits": 3720,
                "per_hour": 0.86,
                "per_day": 20.7,
                "per_week": 144.7,
                "per_month": 620.0,
            },
            {
                "label": "365 days",
                "commits": 3871,
                "per_hour": 0.44,
                "per_day": 10.6,
                "per_week": 74.2,
                "per_month": 318.2,
            },
        ]

        # Act
        output = generate_images._commit_velocity_table(windows)

        # Assert
        self.assertEqual(output.count('text-anchor="middle"'), 16)
        self.assertEqual(output.count('text-anchor="start"'), 4)
        self.assertNotIn('text-anchor="end"', output)
        self.assertIn('x="21" y="86" text-anchor="start">Window</text>', output)
        self.assertIn('x="140" y="86" text-anchor="middle">/ hr</text>', output)
        self.assertIn('x="316" y="86" text-anchor="middle">/ mo</text>', output)
        self.assertIn(
            '<line class="table-divider" x1="21" y1="96" x2="339" y2="96" />',
            output,
        )
        self.assertIn(
            'class="label" x="21" y="118" text-anchor="start">30 days</text>',
            output,
        )
        self.assertIn(
            'class="value" x="140" y="118" text-anchor="middle">0.50</text>',
            output,
        )
        self.assertIn(
            'class="value" x="316" y="118" text-anchor="middle">360.0</text>',
            output,
        )

    def test_experimental_stack_bar_matches_language_progress_style(self):
        # Arrange
        values = [
            ("Commits", 3, "#2da44e"),
            ("Pull requests", 1, "#0969da"),
            ("Issues", 2, "#bf8700"),
        ]

        # Act
        stack = generate_images._experimental_stack_bar(values)
        template = Path("templates/experimental-contribution-mix.svg").read_text(
            encoding="utf-8"
        )

        # Assert
        self.assertIn('class="stack-track"', stack)
        self.assertIn('class="metric-bar stack-segment"', stack)
        self.assertIn('y="70"', stack)
        self.assertIn('height="8"', stack)
        self.assertIn('rx="6"', stack)
        self.assertIn(".stack-track { fill: rgb(225, 228, 232); }", template)
        self.assertIn(
            "#gh-dark-mode-only:target .stack-track "
            "{ fill: rgba(110, 118, 129, 0.4); }",
            template,
        )
        self.assertIn(
            ".stack-segment { stroke: rgb(225, 228, 232); stroke-width: 2; }",
            template,
        )
        self.assertIn(
            "#gh-dark-mode-only:target .stack-segment { stroke: #393f47; }",
            template,
        )
        segment_widths = [
            int(width)
            for width in re.findall(
                r'<rect class="metric-bar stack-segment"[^>]* width="(\d+)"',
                stack,
            )
        ]
        self.assertEqual(sum(segment_widths), 318)

    def test_contribution_mix_places_rows_close_to_stack_bar(self):
        # Arrange
        metrics = {
            "contribution_mix": {
                "commits": 3,
                "pull_requests": 1,
                "pull_request_reviews": 0,
                "issues": 2,
                "repositories": 1,
                "restricted": 0,
                "total": 7,
            }
        }

        with TemporaryDirectory() as tmpdir:
            previous_cwd = Path.cwd()
            tmp_path = Path(tmpdir)
            (tmp_path / "templates").mkdir()
            (tmp_path / "generated").mkdir()
            shutil.copy(
                previous_cwd / "templates" / "experimental-contribution-mix.svg",
                tmp_path / "templates" / "experimental-contribution-mix.svg",
            )

            try:
                os.chdir(tmp_path)

                # Act
                generate_images._generate_experimental_contribution_mix(metrics)

                # Assert
                output = (
                    tmp_path / "generated" / "experimental-contribution-mix.svg"
                ).read_text(encoding="utf-8")
                self.assertIn('y="70" width="318" height="8"', output)
                self.assertIn('<text class="label" x="21" y="98">Commits</text>', output)
            finally:
                os.chdir(previous_cwd)

    def test_language_icon_normalization_maps_clear_github_languages(self):
        # Act / Assert
        self.assertEqual(language_icons.normalize_language_icon_slug("Shell"), "bash")
        self.assertEqual(language_icons.normalize_language_icon_slug("CSS"), "css3")
        self.assertEqual(language_icons.normalize_language_icon_slug("C++"), "cplusplus")
        self.assertEqual(language_icons.normalize_language_icon_slug("C#"), "csharp")
        self.assertEqual(
            language_icons.normalize_language_icon_slug("Dockerfile"),
            "docker",
        )
        self.assertEqual(language_icons.normalize_language_icon_slug("Go"), "go")
        self.assertEqual(language_icons.normalize_language_icon_slug("SCSS"), "sass")
        self.assertEqual(language_icons.normalize_language_icon_slug("Swift"), "swift")
        self.assertEqual(language_icons.normalize_language_icon_slug("Kotlin"), "kotlin")
        self.assertEqual(language_icons.normalize_language_icon_slug("PHP"), "php")
        self.assertEqual(
            language_icons.normalize_language_icon_slug("Jupyter Notebook"),
            "jupyter",
        )
        self.assertEqual(language_icons.normalize_language_icon_slug("YAML"), "yaml")
        self.assertEqual(language_icons.normalize_language_icon_slug("Svelte"), "svelte")
        self.assertEqual(
            language_icons.normalize_language_icon_slug("WebAssembly"),
            "wasm",
        )
        self.assertEqual(
            language_icons.normalize_language_icon_slug("Objective-C"),
            "objectivec",
        )
        self.assertEqual(
            language_icons.normalize_language_icon_slug("PLpgSQL"),
            "postgresql",
        )
        self.assertEqual(
            language_icons.normalize_language_icon_slug("TSQL"),
            "microsoftsqlserver",
        )
        self.assertIsNone(language_icons.normalize_language_icon_slug("HCL"))
        self.assertIsNone(language_icons.normalize_language_icon_slug("Go Template"))

    def test_language_icon_renderer_uses_vendored_svg_for_mapped_languages(self):
        # Act
        icon = language_icons.render_language_icon("TypeScript", "#3178c6")

        # Assert
        self.assertIn("<svg", icon)
        self.assertIn('class="language-icon"', icon)
        self.assertNotIn("language-icon-fallback", icon)
        self.assertNotIn("language-icon-dark-outline", icon)
        self.assertNotIn("href=", icon)
        self.assertNotIn("cdn.", icon)

    def test_language_icon_renderer_outlines_dark_icons_by_slug(self):
        # Act
        rust_icon = language_icons.render_language_icon("Rust", "#dea584")
        shell_icon = language_icons.render_language_icon("Shell", "#89e051")
        bash_icon = language_icons.render_language_icon("Bash", "#89e051")
        typescript_icon = language_icons.render_language_icon("TypeScript", "#3178c6")

        # Assert
        self.assertIn("language-icon-dark-outline", rust_icon)
        self.assertIn("language-icon-dark-outline", shell_icon)
        self.assertIn("language-icon-dark-outline", bash_icon)
        self.assertNotIn("language-icon-dark-outline", typescript_icon)

    def test_all_vendored_language_icons_pass_sanitization(self):
        # Arrange
        language_icons._load_sanitized_icon.cache_clear()

        # Act / Assert
        for slug in language_icons.LANGUAGE_ICON_FILES:
            with self.subTest(slug=slug):
                icon = language_icons._load_sanitized_icon(slug)
                self.assertIsNotNone(icon)
                self.assertNotRegex(icon, r'href="https?:')
                self.assertNotRegex(icon, r"href='https?:")
                self.assertNotIn("cdn.", icon)
                self.assertNotIn("javascript:", icon)

    def test_language_icon_renderer_falls_back_to_colored_dot(self):
        # Act
        icon = language_icons.render_language_icon("Just", "#384d54")

        # Assert
        self.assertIn("language-icon-fallback", icon)
        self.assertIn("fill:#384d54", icon)
        self.assertIn("M8 4a4 4", icon)

    def test_language_icon_sanitizer_rejects_unsafe_svg(self):
        # Arrange
        root = ET.fromstring(
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<script>alert(1)</script>'
            "</svg>"
        )

        # Act / Assert
        with self.assertRaises(ValueError):
            language_icons._sanitize_svg_tree(root, "unsafe")

    async def test_generate_languages_renders_icons_without_external_refs(self):
        # Arrange
        class LanguageStats:
            @property
            async def languages(self):
                return {
                    "Rust": {"size": 10, "prop": 90, "color": "#dea584"},
                    "Just": {"size": 1, "prop": 10, "color": "#384d54"},
                }

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "templates").mkdir()
            (tmp_path / "generated").mkdir()
            (tmp_path / "templates" / "languages.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg">'
                "{{ progress }}{{ lang_list }}</svg>",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()

            try:
                os.chdir(tmp_path)

                # Act
                await generate_images.generate_languages(LanguageStats())

                # Assert
                output = (tmp_path / "generated" / "languages.svg").read_text(
                    encoding="utf-8"
                )
                self.assertIn("language-icon language-icon-dark-outline", output)
                self.assertIn("language-icon-fallback", output)
                self.assertNotIn(
                    "language-icon-fallback language-icon-dark-outline",
                    output,
                )
                self.assertIn("<span class=\"lang\">Rust</span>", output)
                self.assertNotIn("href=", output)
                self.assertNotIn("cdn.", output)
            finally:
                os.chdir(previous_cwd)

    def test_repo_portfolio_truncates_long_visible_labels(self):
        # Arrange
        long_repo = "pRizz/iota-transaction-spammer-webapp"
        bars = generate_images._experimental_horizontal_bars(
            [{"display_name": long_repo, "score": 75}],
            "display_name",
            "score",
            label_max_chars=23,
            bar_max_width=96,
        )

        # Act
        visible_text = re.findall(r"<text[^>]*>(.*?)</text>", bars)

        # Assert
        self.assertNotIn(long_repo, visible_text)
        self.assertIn("pRizz/iota-transacti...", visible_text)
        self.assertIn(f"<title>{long_repo}: 75</title>", bars)

    def test_horizontal_bar_columns_keep_bars_before_values(self):
        # Arrange
        bars = generate_images._experimental_horizontal_bars(
            [{"language": "Rust", "raw_percent": 100, "display_value": "100.0%"}],
            "language",
            "raw_percent",
            bar_x=182,
            bar_max_width=96,
            value_x=330,
        )

        # Act
        rect_match = re.search(
            r'<rect class="metric-bar" x="(\d+)" y="\d+" width="(\d+)"',
            bars,
        )
        value_match = re.search(r'<text class="value" x="(\d+)"', bars)

        # Assert
        self.assertIsNotNone(rect_match)
        self.assertIsNotNone(value_match)
        bar_end = int(rect_match.group(1)) + int(rect_match.group(2))
        value_x = int(value_match.group(1))
        self.assertLessEqual(bar_end, 278)
        self.assertLess(bar_end, value_x)

    def test_horizontal_bar_can_place_values_before_bars(self):
        # Arrange
        bars = generate_images._experimental_horizontal_bars(
            [{"language": "Rust", "raw_percent": 100, "display_value": "100.0%"}],
            "language",
            "raw_percent",
            bar_x=182,
            bar_max_width=96,
            value_x=170,
            value_position="before_bar",
        )

        # Act
        value_match = re.search(r'<text class="value" x="(\d+)"', bars)
        rect_match = re.search(r'<rect class="metric-bar" x="(\d+)"', bars)
        value_index = bars.index('<text class="value"')
        rect_index = bars.index('<rect class="metric-bar"')

        # Assert
        self.assertIsNotNone(value_match)
        self.assertIsNotNone(rect_match)
        self.assertLess(int(value_match.group(1)), int(rect_match.group(1)))
        self.assertLess(value_index, rect_index)

    def test_horizontal_bar_language_icons_shift_labels_without_overlap(self):
        # Arrange
        bars = generate_images._experimental_horizontal_bars(
            [
                {
                    "language": "Rust",
                    "raw_percent": 100,
                    "display_value": "100.0%",
                    "color": "#dea584",
                }
            ],
            "language",
            "raw_percent",
            bar_x=182,
            bar_max_width=96,
            value_x=170,
            value_position="before_bar",
            label_icon_key="language",
            label_icon_color_key="color",
        )

        # Act
        icon_tag = re.search(r'<svg[^>]*experimental-language-icon[^>]*>', bars)
        label_match = re.search(r'<text class="label" x="(\d+)"', bars)
        value_match = re.search(r'<text class="value" x="(\d+)"', bars)
        rect_match = re.search(r'<rect class="metric-bar" x="(\d+)"', bars)

        # Assert
        self.assertIsNotNone(icon_tag)
        self.assertIn("language-icon-dark-outline", icon_tag.group(0))
        self.assertIsNotNone(label_match)
        self.assertIsNotNone(value_match)
        self.assertIsNotNone(rect_match)
        icon_x = int(re.search(r' x="(\d+)"', icon_tag.group(0)).group(1))
        icon_y = int(re.search(r' y="(\d+)"', icon_tag.group(0)).group(1))
        icon_width = int(re.search(r' width="(\d+)"', icon_tag.group(0)).group(1))
        icon_end = icon_x + icon_width
        label_x = int(label_match.group(1))
        value_x = int(value_match.group(1))
        rect_x = int(rect_match.group(1))
        self.assertEqual(63, icon_y)
        self.assertLess(icon_end, label_x)
        self.assertLess(label_x, value_x)
        self.assertLess(value_x, rect_x)

    def test_primary_language_row_renders_icon_next_to_value(self):
        # Arrange
        rows = generate_images._experimental_rows(
            [("Primary language", "Rust")],
            value_icon_labels={"Primary language"},
        )

        # Act
        icon_tag = re.search(r'<svg[^>]*experimental-language-icon[^>]*>', rows)
        value_match = re.search(r'<text class="value" x="(\d+)"', rows)

        # Assert
        self.assertIsNotNone(icon_tag)
        self.assertIn("language-icon-dark-outline", icon_tag.group(0))
        self.assertIsNotNone(value_match)
        icon_x = int(re.search(r' x="(\d+)"', icon_tag.group(0)).group(1))
        icon_y = int(re.search(r' y="(\d+)"', icon_tag.group(0)).group(1))
        icon_width = int(re.search(r' width="(\d+)"', icon_tag.group(0)).group(1))
        self.assertEqual(65, icon_y)
        self.assertLess(
            icon_x + icon_width,
            int(value_match.group(1)),
        )

    def test_repo_portfolio_shortens_large_values_with_title(self):
        # Arrange
        long_repo = "pRizz/SVG-Navigator---Chrome-Extension"
        bars = generate_images._experimental_horizontal_bars(
            [
                {
                    "display_name": long_repo,
                    "score": 123456789,
                    "display_value": "123,456,789",
                }
            ],
            "display_name",
            "score",
            label_max_chars=23,
            value_max_chars=8,
            bar_max_width=96,
        )

        # Act
        visible_text = re.findall(r"<text[^>]*>(.*?)</text>", bars)

        # Assert
        self.assertNotIn(long_repo, visible_text)
        self.assertIn("pRizz/SVG-Navigator-...", visible_text)
        self.assertIn("123,4...", visible_text)
        self.assertIn(f"<title>{long_repo}: 123,456,789</title>", bars)

    def test_contribution_mix_rows_stay_inside_card(self):
        # Arrange
        template = """<svg id="gh-dark-mode-only" width="360" height="210" xmlns="http://www.w3.org/2000/svg">
<text>{{ title }}</text><text>{{ subtitle }}</text>{{ body }}</svg>"""
        metrics = {
            "contribution_mix": {
                "commits": 4284,
                "pull_requests": 215,
                "pull_request_reviews": 2,
                "issues": 23,
                "repositories": 84,
                "restricted": 0,
                "total": 4608,
            }
        }
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "templates").mkdir()
            (tmp_path / "templates" / "experimental-contribution-mix.svg").write_text(
                template,
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()

            try:
                os.chdir(tmp_path)

                # Act
                generate_images._generate_experimental_contribution_mix(metrics)
                output = (
                    tmp_path / "generated" / "experimental-contribution-mix.svg"
                ).read_text(encoding="utf-8")
            finally:
                os.chdir(previous_cwd)

        # Assert
        row_y_values = [
            int(value)
            for value in re.findall(
                r'<text class="label" x="21" y="(\d+)">', output
            )
        ]
        row_labels = re.findall(
            r'<text class="label" x="21" y="\d+">(.*?)</text>',
            output,
        )
        stack_titles = re.findall(r"<title>(.*?):", output)
        self.assertEqual(max(row_y_values), 178)
        self.assertEqual(
            row_labels,
            [
                "Commits",
                "Pull requests",
                "Repositories",
                "Issues",
                "Reviews",
                "Restricted",
            ],
        )
        self.assertEqual(
            stack_titles,
            ["Commits", "Pull requests", "Repositories", "Issues", "Reviews"],
        )
        self.assertIn(">Restricted</text>", output)
        self.assertIn("<rect", output)
        self.assertNotIn("<title>Restricted:", output)

    def test_limited_data_suffix_removed_from_selected_experimental_cards(self):
        # Arrange
        template = """<svg id="gh-dark-mode-only" width="360" height="210" xmlns="http://www.w3.org/2000/svg">
<text>{{ title }}</text><text>{{ subtitle }}</text>{{ body }}</svg>"""
        metrics = {
            "limited_data": ["monthly commit scan data degraded"],
            "contribution_pulse": {
                "total": 42,
                "active_months": 3,
                "month_count": 13,
                "average_monthly": 3.2,
                "best_month": "Apr '26",
                "best_month_count": 20,
                "current_month": "May '26",
                "current_month_count": 7,
                "active_streak_months": 2,
            },
            "collaboration": {
                "merged_pull_requests": 5,
                "pull_request_reviews": 8,
                "issues": 13,
                "owned_repositories": 21,
                "external_repositories": 3,
            },
        }
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "templates").mkdir()
            for name in ("contribution-pulse", "collaboration"):
                (tmp_path / "templates" / f"experimental-{name}.svg").write_text(
                    template,
                    encoding="utf-8",
                )
            previous_cwd = Path.cwd()

            try:
                os.chdir(tmp_path)

                # Act
                generate_images._generate_experimental_contribution_pulse(metrics)
                generate_images._generate_experimental_collaboration(metrics)
                contribution_pulse = (
                    tmp_path / "generated" / "experimental-contribution-pulse.svg"
                ).read_text(encoding="utf-8")
                collaboration = (
                    tmp_path / "generated" / "experimental-collaboration.svg"
                ).read_text(encoding="utf-8")
            finally:
                os.chdir(previous_cwd)

        # Assert
        self.assertIn("Recent activity — last 13 months", contribution_pulse)
        self.assertIn("Current-year contribution signals", collaboration)
        self.assertNotIn("; limited data", contribution_pulse)
        self.assertNotIn("; limited data", collaboration)

    async def test_generate_experimental_renders_all_cards_without_placeholders(self):
        # Arrange
        template = """<svg id="gh-dark-mode-only" width="360" height="210" xmlns="http://www.w3.org/2000/svg">
<text>{{ title }}</text><text>{{ subtitle }}</text>{{ body }}</svg>"""
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "templates").mkdir()
            (tmp_path / "generated").mkdir()
            (tmp_path / "generated" / "monthly-commits.json").write_text(
                generate_images.json.dumps(
                    {
                        "months": [
                            {
                                "key": "2026-05",
                                "label": "May '26",
                                "count": 7,
                                "is_current": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            for name in generate_images.EXPERIMENTAL_CARD_NAMES:
                (tmp_path / "templates" / f"experimental-{name}.svg").write_text(
                    template,
                    encoding="utf-8",
                )
            (tmp_path / "templates" / "experimental-star-history.svg").write_text(
                template,
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()

            try:
                os.chdir(tmp_path)

                # Act
                await generate_images.generate_experimental(_ExperimentalStats())

                # Assert
                for name in generate_images.EXPERIMENTAL_CARD_NAMES:
                    output = (
                        tmp_path / "generated" / f"experimental-{name}.svg"
                    ).read_text(encoding="utf-8")
                    self.assertNotIn("{{", output)
                    self.assertNotIn("}}", output)
                    self.assertIn("<svg", output)
                    self.assertNotIn("octocat/secret", output)
                language_momentum = (
                    tmp_path / "generated" / "experimental-language-momentum.svg"
                ).read_text(encoding="utf-8")
                trading_card = (
                    tmp_path / "generated" / "experimental-trading-card.svg"
                ).read_text(encoding="utf-8")
                commit_velocity = (
                    tmp_path / "generated" / "experimental-commit-velocity.svg"
                ).read_text(encoding="utf-8")
                star_history = (
                    tmp_path / "generated" / "experimental-star-history-30-days.svg"
                ).read_text(encoding="utf-8")
                self.assertIn("experimental-language-icon", language_momentum)
                self.assertIn("experimental-language-icon", trading_card)
                self.assertIn("language-icon-dark-outline", language_momentum)
                self.assertIn("Commit Velocity", commit_velocity)
                self.assertIn("/ mo", commit_velocity)
                self.assertIn("6 months", commit_velocity)
                self.assertIn("Owned Stars: 30 days", star_history)
                self.assertIn("Collecting daily star snapshots", star_history)
                self.assertNotIn("href=", language_momentum)
                self.assertNotIn("href=", trading_card)
                metrics = (
                    tmp_path / "generated" / "experimental-metrics.json"
                ).read_text(encoding="utf-8")
                self.assertNotIn("octocat/secret", metrics)
                self.assertFalse(
                    (
                        tmp_path
                        / "generated"
                        / "experimental-code-footprint.svg"
                    ).exists()
                )
            finally:
                os.chdir(previous_cwd)

    def test_generation_sources_do_not_call_contributor_stats_endpoint(self):
        # Arrange
        retired_endpoint = "stats/" + "contributors"
        source_paths = ("generate_images.py", "github_stats.py")

        # Act
        source_text = "\n".join(
            Path(path).read_text(encoding="utf-8") for path in source_paths
        )

        # Assert
        self.assertNotIn(retired_endpoint, source_text)

    def test_validate_generated_output_checks_experimental_when_enabled(self):
        # Arrange
        with TemporaryDirectory() as tmpdir:
            generated = Path(tmpdir)
            (generated / "overview.svg").write_text(
                "Octocat Owned stars</td><td>1</td> Owned forks</td><td>2</td> "
                "Repositories with contributions</td><td>1</td>",
                encoding="utf-8",
            )
            (generated / "languages.svg").write_text(
                '<span class="progress-item"></span>',
                encoding="utf-8",
            )
            (generated / "monthly-commits.svg").write_text(
                'Monthly Commits <rect class="bar" />',
                encoding="utf-8",
            )
            for name in generate_images.EXPERIMENTAL_CARD_NAMES:
                (generated / f"experimental-{name}.svg").write_text(
                    "<svg>ok</svg>",
                    encoding="utf-8",
                )

            # Act / Assert
            with mock.patch.dict("os.environ", {"GENERATE_EXPERIMENTAL": "true"}):
                generate_images.validate_generated_output(str(generated))

    def test_validate_generated_output_skips_experimental_when_disabled(self):
        # Arrange
        with TemporaryDirectory() as tmpdir:
            generated = Path(tmpdir)
            (generated / "overview.svg").write_text(
                "Octocat Owned stars</td><td>1</td> Owned forks</td><td>2</td> "
                "Repositories with contributions</td><td>1</td>",
                encoding="utf-8",
            )
            (generated / "languages.svg").write_text(
                '<span class="progress-item"></span>',
                encoding="utf-8",
            )
            (generated / "monthly-commits.svg").write_text(
                'Monthly Commits <rect class="bar" />',
                encoding="utf-8",
            )

            # Act / Assert
            with mock.patch.dict("os.environ", {"GENERATE_EXPERIMENTAL": "false"}):
                generate_images.validate_generated_output(str(generated))

    async def test_build_run_report_includes_new_metrics_without_lines_changed(self):
        # Act
        report = await generate_images.build_run_report(_ReportStats())

        # Assert
        self.assertEqual(report["stats"]["current_year_contributions"], 4)
        self.assertEqual(report["stats"]["merged_pull_requests"], 5)
        self.assertNotIn("lines_changed", report["stats"])
        self.assertEqual(report["stats"]["views"], 6)
        self.assertIn("star_history", report)
        self.assertIn("samples", report["star_history"])
        self.assertIn("card_count", report["star_history"])

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
            "star_history": {
                "samples": 3,
                "latest_date": "2026-06-05",
                "latest_total": 13,
                "card_count": 3,
            },
            "api": {
                "total_repos": 4,
                "traffic_degraded": [
                    {
                        "repo": "octocat/private",
                        "endpoint": "traffic/views",
                        "status": 403,
                        "category": "auth_or_permission_error",
                        "message": "traffic views inaccessible",
                        "attempts": 3,
                        "retry_count": 2,
                        "wait_seconds": 4.3,
                        "elapsed_seconds": 4.5,
                    }
                ],
                "critical_failures": [],
                "rest_requests_total": 1,
                "rest_retries_total": 2,
                "rest_wait_seconds_total": 4.3,
                "traffic_wait_seconds_total": 4.3,
                "monthly_commits_degraded": [],
                "monthly_commits_wait_seconds_total": 0.0,
                "slowest_requests": [
                    {
                        "repo": "octocat/private",
                        "endpoint": "traffic/views",
                        "status": 403,
                        "category": "auth_or_permission_error",
                        "degraded": True,
                        "attempts": 3,
                        "retry_count": 2,
                        "wait_seconds": 4.3,
                        "elapsed_seconds": 4.5,
                        "message": "traffic views inaccessible",
                    }
                ],
            },
        }

        # Act
        summary = generate_images.render_action_summary(report)

        # Assert
        self.assertIn("octocat/private", summary)
        self.assertIn("Traffic views degraded: 1", summary)
        self.assertIn("Current-year contributions", summary)
        self.assertIn("Merged pull requests", summary)
        self.assertIn("API Wait Summary", summary)
        self.assertIn("Slowest API Waits", summary)
        self.assertIn("Star history samples: 3", summary)
        self.assertIn("Latest star snapshot: 2026-06-05 (13)", summary)
        self.assertIn("Star history cards: 3", summary)
        self.assertIn("4.3s", summary)
        self.assertNotIn("Contributor stats", summary)

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

    def test_env_file_loader_loads_simple_assignments(self):
        # Arrange
        with TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "ACCESS_TOKEN=token-from-file\n"
                "GITHUB_ACTOR=octocat\n"
                "EXCLUDED=octocat/one,octocat/two\n",
                encoding="utf-8",
            )

            # Act
            with mock.patch.dict("os.environ", {}, clear=True):
                generate_images.load_env_file(str(env_path))

                # Assert
                self.assertEqual(os.environ["ACCESS_TOKEN"], "token-from-file")
                self.assertEqual(os.environ["GITHUB_ACTOR"], "octocat")
                self.assertEqual(os.environ["EXCLUDED"], "octocat/one,octocat/two")

    def test_env_file_loader_ignores_comments_and_blank_lines(self):
        # Arrange
        with TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n# local secrets\nACCESS_TOKEN=token\n\n",
                encoding="utf-8",
            )

            # Act
            with mock.patch.dict("os.environ", {}, clear=True):
                generate_images.load_env_file(str(env_path))

                # Assert
                self.assertEqual(os.environ, {"ACCESS_TOKEN": "token"})

    def test_env_file_loader_preserves_existing_environment_values(self):
        # Arrange
        with TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "ACCESS_TOKEN=token-from-file\nGITHUB_ACTOR=from-file\n",
                encoding="utf-8",
            )

            # Act
            with mock.patch.dict(
                "os.environ",
                {"ACCESS_TOKEN": "token-from-shell"},
                clear=True,
            ):
                generate_images.load_env_file(str(env_path))

                # Assert
                self.assertEqual(os.environ["ACCESS_TOKEN"], "token-from-shell")
                self.assertEqual(os.environ["GITHUB_ACTOR"], "from-file")

    def test_env_file_loader_strips_optional_quotes(self):
        # Arrange
        with TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "ACCESS_TOKEN='single-quoted'\n"
                'GITHUB_ACTOR="double-quoted"\n',
                encoding="utf-8",
            )

            # Act
            with mock.patch.dict("os.environ", {}, clear=True):
                generate_images.load_env_file(str(env_path))

                # Assert
                self.assertEqual(os.environ["ACCESS_TOKEN"], "single-quoted")
                self.assertEqual(os.environ["GITHUB_ACTOR"], "double-quoted")

    def test_env_file_loader_allows_missing_file(self):
        # Act / Assert
        with mock.patch.dict("os.environ", {}, clear=True):
            generate_images.load_env_file("/tmp/github-stats-missing-env-file")
            self.assertEqual(os.environ, {})

    async def test_main_loads_access_token_and_actor_from_env_file(self):
        # Arrange
        _FakeStats.loaded = False
        _FakeStats.get_stats_calls = 0
        _FakeStats.init_args = None

        async def assert_loaded_before_render(stats):
            self.assertTrue(type(stats).loaded)

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / ".env").write_text(
                "ACCESS_TOKEN=token-from-env-file\n"
                "GITHUB_ACTOR=env-file-user\n"
                "GENERATE_EXPERIMENTAL=false\n",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()

            try:
                os.chdir(tmp_path)

                # Act
                with mock.patch.dict("os.environ", {}, clear=True), mock.patch(
                    "generate_images.Stats", _FakeStats
                ), mock.patch(
                    "generate_images.generate_languages", assert_loaded_before_render
                ), mock.patch(
                    "generate_images.generate_overview", assert_loaded_before_render
                ), mock.patch(
                    "generate_images.generate_monthly_commits",
                    assert_loaded_before_render,
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
                                "traffic_degraded": [],
                                "monthly_commits_degraded": [],
                                "critical_failures": [],
                                "rest_requests_total": 0,
                                "rest_retries_total": 0,
                                "rest_wait_seconds_total": 0.0,
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
                ), mock.patch(
                    "generate_images.generate_experimental",
                    mock.AsyncMock(side_effect=AssertionError("disabled")),
                ):
                    await generate_images.main()

                # Assert
                args, _kwargs = _FakeStats.init_args
                self.assertEqual(args[0], "env-file-user")
                self.assertEqual(args[1], "token-from-env-file")
                self.assertEqual(_FakeStats.get_stats_calls, 1)
                history = generate_images.load_star_history_cache(
                    str(tmp_path / "generated" / "star-history.csv")
                )
                self.assertEqual(history[0]["total_stargazers"], 17)
                self.assertEqual(history[0]["repo_count"], 2)
            finally:
                os.chdir(previous_cwd)

    def test_action_summary_writer_allows_missing_github_step_summary(self):
        # Act / Assert
        with mock.patch.dict("os.environ", {}, clear=True):
            generate_images.write_action_summary("# Summary")


if __name__ == "__main__":
    unittest.main()
