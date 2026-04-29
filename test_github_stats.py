#!/usr/bin/python3

import unittest
from unittest import mock

import requests

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
    def __init__(self, status_code=502):
        self.status_code = status_code
        self.text = "Bad Gateway"
        self.headers = {}

    def json(self):
        raise requests.exceptions.JSONDecodeError("Expecting value", "", 0)


class _Always202Response:
    status_code = 202
    text = ""
    headers = {}


class _ConcurrentContributorQueries:
    def __init__(self):
        self.active_requests = 0
        self.max_active_requests = 0

    async def query_rest(self, path, params=None, max_wait_seconds=None):
        self.active_requests += 1
        self.max_active_requests = max(self.max_active_requests, self.active_requests)
        try:
            await generate_images.asyncio.sleep(0.01)
            return [
                {
                    "author": {"login": "octocat"},
                    "weeks": [{"a": 10, "d": 4}],
                }
            ]
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


class GithubStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_graphql_query_returns_empty_dict_for_non_json_fallback(self):
        # Arrange
        queries = Queries("octocat", "token", _FailingSession())

        with mock.patch(
            "github_stats.requests.post", return_value=_FakeRequestsResponse()
        ), mock.patch("github_stats.asyncio.sleep", mock.AsyncMock()):
            # Act
            result = await queries.query("query { viewer { login } }")

        # Assert
        self.assertEqual(result, {})

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
        ):
            await generate_images.main()

        # Assert
        self.assertEqual(_FakeStats.get_stats_calls, 1)


if __name__ == "__main__":
    unittest.main()
