#!/usr/bin/python3

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple, Any, cast

import aiohttp
import requests


def _empty_rest_value(normalized_path: str) -> Any:
    """JSON shape GitHub would return for an empty successful response."""
    if "stats/contributors" in normalized_path:
        return []
    return {}


@dataclass(frozen=True)
class _StatusDecision:
    category: str
    retryable: bool
    message: str


def _header_value(headers: Any, name: str) -> Optional[str]:
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    if value is None:
        value = headers.get(name.lower())
    if value is None:
        return None
    return str(value)


def _parse_float_header(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _retry_delay(headers: Any, attempt: int, default: float = 2.0) -> float:
    """
    Seconds to wait before retrying. Prefer GitHub's rate-limit headers.
    """
    retry_after = _parse_float_header(_header_value(headers, "Retry-After"))
    if retry_after is not None:
        return max(1.0, min(retry_after, 60.0))

    reset_epoch = _parse_float_header(_header_value(headers, "x-ratelimit-reset"))
    remaining = _header_value(headers, "x-ratelimit-remaining")
    if reset_epoch is not None and remaining == "0":
        return max(1.0, min(reset_epoch - time.time(), 60.0))

    return min(default * (1.15 ** min(attempt - 1, 10)), 20.0)


def _delay_for_202(retry_after_header: Optional[str], attempt: int) -> float:
    """
    Seconds to wait before retrying a 202. Prefer GitHub's Retry-After header.
    """
    return _retry_delay({"Retry-After": retry_after_header}, attempt)


def _looks_rate_limited(status: int, headers: Any, body_preview: str) -> bool:
    if status not in {403, 429}:
        return False
    if _header_value(headers, "Retry-After") is not None:
        return True
    if _header_value(headers, "x-ratelimit-remaining") == "0":
        return True

    lowered_preview = body_preview.lower()
    return "rate limit" in lowered_preview or "too many requests" in lowered_preview


def _classify_status(
    status: int,
    headers: Any,
    body_preview: str,
    endpoint_kind: str,
) -> _StatusDecision:
    if status in {200, 201}:
        return _StatusDecision("success_with_json", False, "response contains JSON")
    if status in {204, 304}:
        return _StatusDecision("success_empty", False, "response has no JSON body")
    if status == 202:
        return _StatusDecision("pending", True, "request accepted but still pending")
    if 300 <= status <= 399:
        location = _header_value(headers, "Location") or "missing Location header"
        return _StatusDecision("redirect", False, f"redirect to {location}")
    if _looks_rate_limited(status, headers, body_preview):
        return _StatusDecision("rate_limited", True, "rate limit response")
    if status == 401 or status == 403:
        return _StatusDecision(
            "auth_or_permission_error", False, "authentication or permission failure"
        )
    if status in {404, 410}:
        return _StatusDecision(
            "not_found_or_gone", False, "resource not found or gone"
        )
    if 400 <= status <= 499:
        return _StatusDecision("client_error", False, "client request error")
    if 500 <= status <= 599:
        return _StatusDecision("server_error", True, "server error response")
    return _StatusDecision(
        "unknown_status", False, f"unexpected {endpoint_kind} HTTP status"
    )


def _is_success_with_json(decision: _StatusDecision) -> bool:
    return decision.category == "success_with_json"


def _is_success_empty(decision: _StatusDecision) -> bool:
    return decision.category == "success_empty"


def _response_preview(text: str) -> str:
    preview = text.strip().replace("\n", " ")
    if len(preview) > 160:
        return f"{preview[:157]}..."
    return preview


async def _async_response_preview(response: Any) -> str:
    text_attr = getattr(response, "text", "")
    if isinstance(text_attr, str):
        return _response_preview(text_attr)
    try:
        return _response_preview(await response.text())
    except Exception:
        return ""


def _sync_response_preview(response: Any) -> str:
    return _response_preview(str(getattr(response, "text", "")))


def _status_error_message(
    endpoint_kind: str,
    status: int,
    decision: _StatusDecision,
    body_preview: str,
) -> str:
    message = f"{endpoint_kind} returned HTTP {status}: {decision.message}"
    if body_preview:
        message = f"{message}; {body_preview}"
    return message


def _can_degrade_rest_status(normalized_path: str, decision: _StatusDecision) -> bool:
    if "stats/contributors" in normalized_path:
        return decision.category in {"pending", "not_found_or_gone"}
    if "traffic/views" in normalized_path:
        return decision.category == "not_found_or_gone"
    return False


def _can_degrade_rest_body(normalized_path: str) -> bool:
    return (
        "stats/contributors" in normalized_path
        or "traffic/views" in normalized_path
    )


def _env_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError:
        print(f"Invalid {name} value {raw_value!r}; using {default}.")
        return default


class GitHubQueryError(RuntimeError):
    """Raised when a required GitHub GraphQL query cannot return valid data."""


###############################################################################
# Main Classes
###############################################################################


class Queries(object):
    """
    Class with functions to query the GitHub GraphQL (v4) API and the REST (v3)
    API. Also includes functions to dynamically generate GraphQL queries.
    """

    def __init__(
        self,
        username: str,
        access_token: str,
        session: aiohttp.ClientSession,
        max_connections: int = 10,
    ):
        self.username = username
        self.access_token = access_token
        self.session = session
        self.semaphore = asyncio.Semaphore(max_connections)

    async def query(self, generated_query: str) -> Dict:
        """
        Make a request to the GraphQL API using the authentication token from
        the environment
        :param generated_query: string query to be sent to the API
        :return: decoded GraphQL JSON output
        """
        headers = {
            "Authorization": f"Bearer {self.access_token}",
        }

        deadline = time.monotonic() + 60.0
        max_iterations = 4

        for attempt in range(1, max_iterations + 1):
            try:
                async with self.semaphore:
                    r_async = await self.session.post(
                        "https://api.github.com/graphql",
                        headers=headers,
                        json={"query": generated_query},
                    )

                preview = await _async_response_preview(r_async)
                decision = _classify_status(
                    r_async.status,
                    r_async.headers,
                    preview,
                    "GraphQL",
                )
                if decision.retryable:
                    delay = _retry_delay(r_async.headers, attempt)
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                    if delay <= 0:
                        break
                    print(
                        f"GraphQL returned {r_async.status} ({decision.category}, "
                        f"attempt {attempt}); retry in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    continue

                if _is_success_empty(decision):
                    raise GitHubQueryError(
                        f"GraphQL returned HTTP {r_async.status} without JSON data"
                    )
                if not _is_success_with_json(decision):
                    raise GitHubQueryError(
                        _status_error_message(
                            "GraphQL", r_async.status, decision, preview
                        )
                    )

                result = await r_async.json()
                if isinstance(result, dict) and result.get("errors") and not result.get(
                    "data"
                ):
                    raise GitHubQueryError(
                        f"GraphQL response contained errors: {result.get('errors')}"
                    )
                if result is not None:
                    return result
            except GitHubQueryError:
                raise
            except Exception as error:
                print(f"aiohttp failed for GraphQL query: {error}")

            r_requests = None
            try:
                async with self.semaphore:
                    r_requests = requests.post(
                        "https://api.github.com/graphql",
                        headers=headers,
                        json={"query": generated_query},
                    )

                preview = _sync_response_preview(r_requests)
                decision = _classify_status(
                    r_requests.status_code,
                    r_requests.headers,
                    preview,
                    "GraphQL fallback",
                )
                if decision.retryable:
                    delay = _retry_delay(r_requests.headers, attempt)
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                    if delay <= 0:
                        break
                    print(
                        f"GraphQL fallback returned {r_requests.status_code} "
                        f"({decision.category}, attempt {attempt}); "
                        f"retry in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    continue

                if _is_success_empty(decision):
                    raise GitHubQueryError(
                        "GraphQL fallback returned no JSON data"
                    )
                if not _is_success_with_json(decision):
                    raise GitHubQueryError(
                        _status_error_message(
                            "GraphQL fallback",
                            r_requests.status_code,
                            decision,
                            preview,
                        )
                    )

                result = r_requests.json()
                if isinstance(result, dict) and result.get("errors") and not result.get(
                    "data"
                ):
                    raise GitHubQueryError(
                        f"GraphQL fallback response contained errors: "
                        f"{result.get('errors')}"
                    )
                if result is not None:
                    return result
            except ValueError as error:
                preview = (
                    "" if r_requests is None else _sync_response_preview(r_requests)
                )
                raise GitHubQueryError(
                    f"GraphQL fallback returned non-JSON: {error}; {preview}"
                ) from error
            except GitHubQueryError:
                raise
            except Exception as error:
                print(f"requests failed for GraphQL query: {error}")

        raise GitHubQueryError("GraphQL query failed after retry budget was exhausted")

    async def query_rest(
        self,
        path: str,
        params: Optional[Dict] = None,
        max_wait_seconds: float = 90.0,
    ) -> Any:
        """
        Make a request to the REST API
        :param path: API path to query
        :param params: Query parameters to be passed to the API
        :param max_wait_seconds: Maximum time to wait on retryable responses
        :return: deserialized REST JSON output
        """

        if params is None:
            params = dict()
        normalized = path[1:] if path.startswith("/") else path

        # GitHub returns 202 while statistics (e.g. contributors) are computed.
        # Without a wall-clock cap, many repos times repeated sleeps can run for hours in CI.
        deadline = time.monotonic() + max_wait_seconds
        max_iterations = 35
        last_status = 0
        last_decision = _StatusDecision("unknown_status", False, "no response yet")
        last_preview = ""

        for attempt in range(1, max_iterations + 1):
            headers = {
                "Authorization": f"token {self.access_token}",
            }
            if max_wait_seconds > 0 and time.monotonic() > deadline:
                if _can_degrade_rest_status(normalized, last_decision):
                    print(
                        f"query_rest: wait budget exhausted for {normalized}; "
                        "using empty data for this endpoint."
                    )
                    return _empty_rest_value(normalized)
                raise GitHubQueryError(
                    _status_error_message(
                        "REST", last_status, last_decision, last_preview
                    )
                )

            try:
                async with self.semaphore:
                    r_async = await self.session.get(
                        f"https://api.github.com/{normalized}",
                        headers=headers,
                        params=tuple(params.items()),
                    )
                preview = await _async_response_preview(r_async)
                decision = _classify_status(
                    r_async.status, r_async.headers, preview, "REST"
                )
                last_status = r_async.status
                last_decision = decision
                last_preview = preview

                if decision.retryable:
                    delay = _retry_delay(r_async.headers, attempt)
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                    if delay <= 0:
                        if _can_degrade_rest_status(normalized, decision):
                            print(
                                f"query_rest: no retry budget for {normalized}; "
                                "using empty data for this endpoint."
                            )
                            return _empty_rest_value(normalized)
                        raise GitHubQueryError(
                            _status_error_message(
                                "REST", r_async.status, decision, preview
                            )
                        )
                    if attempt <= 2 or attempt % 6 == 0:
                        print(
                            f"{normalized} returned {r_async.status} "
                            f"({decision.category}, attempt {attempt}); "
                            f"retry in {delay:.1f}s"
                        )
                    await asyncio.sleep(delay)
                    continue

                if _is_success_empty(decision):
                    return _empty_rest_value(normalized)
                if not _is_success_with_json(decision):
                    if _can_degrade_rest_status(normalized, decision):
                        print(
                            f"{normalized} returned {r_async.status} "
                            f"({decision.category}); using empty endpoint data."
                        )
                        return _empty_rest_value(normalized)
                    raise GitHubQueryError(
                        _status_error_message(
                            "REST", r_async.status, decision, preview
                        )
                    )

                try:
                    result = await r_async.json()
                except ValueError as error:
                    if _can_degrade_rest_body(normalized):
                        print(
                            f"{normalized} returned non-JSON: {error}; "
                            "using empty endpoint data."
                        )
                        return _empty_rest_value(normalized)
                    raise GitHubQueryError(
                        f"{normalized} returned non-JSON: {error}; {preview}"
                    ) from error
                if result is not None:
                    return result
            except GitHubQueryError:
                raise
            except Exception as error:
                print(f"aiohttp failed for rest query: {error}")

            r_requests = None
            try:
                async with self.semaphore:
                    r_requests = requests.get(
                        f"https://api.github.com/{normalized}",
                        headers=headers,
                        params=tuple(params.items()),
                    )
                preview = _sync_response_preview(r_requests)
                decision = _classify_status(
                    r_requests.status_code,
                    r_requests.headers,
                    preview,
                    "REST fallback",
                )
                last_status = r_requests.status_code
                last_decision = decision
                last_preview = preview

                if decision.retryable:
                    delay = _retry_delay(r_requests.headers, attempt)
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                    if delay <= 0:
                        if _can_degrade_rest_status(normalized, decision):
                            return _empty_rest_value(normalized)
                        raise GitHubQueryError(
                            _status_error_message(
                                "REST fallback",
                                r_requests.status_code,
                                decision,
                                preview,
                            )
                        )
                    if attempt <= 2 or attempt % 6 == 0:
                        print(
                            f"{normalized} returned {r_requests.status_code} "
                            f"via fallback ({decision.category}, "
                            f"attempt {attempt}); retry in {delay:.1f}s"
                        )
                    await asyncio.sleep(delay)
                    continue

                if _is_success_empty(decision):
                    return _empty_rest_value(normalized)
                if not _is_success_with_json(decision):
                    if _can_degrade_rest_status(normalized, decision):
                        print(
                            f"{normalized} returned {r_requests.status_code} "
                            f"via fallback ({decision.category}); "
                            "using empty endpoint data."
                        )
                        return _empty_rest_value(normalized)
                    raise GitHubQueryError(
                        _status_error_message(
                            "REST fallback",
                            r_requests.status_code,
                            decision,
                            preview,
                        )
                    )

                try:
                    result = r_requests.json()
                except ValueError as error:
                    if _can_degrade_rest_body(normalized):
                        print(
                            f"{normalized} fallback returned non-JSON: "
                            f"{error}; {preview}"
                        )
                        return _empty_rest_value(normalized)
                    raise GitHubQueryError(
                        f"{normalized} fallback returned non-JSON: "
                        f"{error}; {preview}"
                    ) from error
                if result is not None:
                    return result
            except GitHubQueryError:
                raise
            except Exception as error:
                print(f"requests failed for rest query: {error}")

        if _can_degrade_rest_status(normalized, last_decision):
            print(
                f"query_rest: too many retryable responses for {normalized}; "
                "data for this endpoint may be incomplete."
            )
            return _empty_rest_value(normalized)
        raise GitHubQueryError(
            _status_error_message("REST", last_status, last_decision, last_preview)
        )

    @staticmethod
    def repos_overview(
        contrib_cursor: Optional[str] = None, owned_cursor: Optional[str] = None
    ) -> str:
        """
        :return: GraphQL query with overview of user repositories
        """
        return f"""{{
  viewer {{
    login,
    name,
    repositories(
        first: 100,
        orderBy: {{
            field: UPDATED_AT,
            direction: DESC
        }},
        isFork: false,
        after: {"null" if owned_cursor is None else '"'+ owned_cursor +'"'}
    ) {{
      pageInfo {{
        hasNextPage
        endCursor
      }}
      nodes {{
        nameWithOwner
        stargazers {{
          totalCount
        }}
        forkCount
        languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{
            size
            node {{
              name
              color
            }}
          }}
        }}
      }}
    }}
    repositoriesContributedTo(
        first: 100,
        includeUserRepositories: false,
        orderBy: {{
            field: UPDATED_AT,
            direction: DESC
        }},
        contributionTypes: [
            COMMIT,
            PULL_REQUEST,
            REPOSITORY,
            PULL_REQUEST_REVIEW
        ]
        after: {"null" if contrib_cursor is None else '"'+ contrib_cursor +'"'}
    ) {{
      pageInfo {{
        hasNextPage
        endCursor
      }}
      nodes {{
        nameWithOwner
        stargazers {{
          totalCount
        }}
        forkCount
        languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{
            size
            node {{
              name
              color
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""

    @staticmethod
    def contrib_years() -> str:
        """
        :return: GraphQL query to get all years the user has been a contributor
        """
        return """
query {
  viewer {
    contributionsCollection {
      contributionYears
    }
  }
}
"""

    @staticmethod
    def contribs_by_year(year: str) -> str:
        """
        :param year: year to query for
        :return: portion of a GraphQL query with desired info for a given year
        """
        return f"""
    year{year}: contributionsCollection(
        from: "{year}-01-01T00:00:00Z",
        to: "{int(year) + 1}-01-01T00:00:00Z"
    ) {{
      contributionCalendar {{
        totalContributions
      }}
    }}
"""

    @classmethod
    def all_contribs(cls, years: List[str]) -> str:
        """
        :param years: list of years to get contributions for
        :return: query to retrieve contribution information for all user years
        """
        by_years = "\n".join(map(cls.contribs_by_year, years))
        return f"""
query {{
  viewer {{
    {by_years}
  }}
}}
"""


class Stats(object):
    """
    Retrieve and store statistics about GitHub usage.
    """

    def __init__(
        self,
        username: str,
        access_token: str,
        session: aiohttp.ClientSession,
        exclude_repos: Optional[Set] = None,
        exclude_langs: Optional[Set] = None,
        ignore_forked_repos: bool = False,
    ):
        self.username = username
        self._ignore_forked_repos = ignore_forked_repos
        self._exclude_repos = set() if exclude_repos is None else exclude_repos
        self._exclude_langs = set() if exclude_langs is None else exclude_langs
        self.queries = Queries(username, access_token, session)

        self._name: Optional[str] = None
        self._stargazers: Optional[int] = None
        self._forks: Optional[int] = None
        self._total_contributions: Optional[int] = None
        self._languages: Optional[Dict[str, Any]] = None
        self._repos: Optional[Set[str]] = None
        self._lines_changed: Optional[Tuple[int, int]] = None
        self._views: Optional[int] = None

    async def to_str(self) -> str:
        """
        :return: summary of all available statistics
        """
        languages = await self.languages_proportional
        formatted_languages = "\n  - ".join(
            [f"{k}: {v:0.4f}%" for k, v in languages.items()]
        )
        lines_changed = await self.lines_changed
        return f"""Name: {await self.name}
Stargazers: {await self.stargazers:,}
Forks: {await self.forks:,}
All-time contributions: {await self.total_contributions:,}
Repositories with contributions: {len(await self.repos)}
Lines of code added: {lines_changed[0]:,}
Lines of code deleted: {lines_changed[1]:,}
Lines of code changed: {lines_changed[0] + lines_changed[1]:,}
Project page views: {await self.views:,}
Languages:
  - {formatted_languages}"""

    async def get_stats(self) -> None:
        """
        Get lots of summary statistics using one big query. Sets many attributes
        """
        stargazers = 0
        forks = 0
        languages: Dict[str, Any] = dict()
        repos_set: Set[str] = set()
        maybe_name: Optional[str] = None

        exclude_langs_lower = {x.lower() for x in self._exclude_langs}

        next_owned = None
        next_contrib = None
        while True:
            raw_results = await self.queries.query(
                Queries.repos_overview(
                    owned_cursor=next_owned, contrib_cursor=next_contrib
                )
            )
            viewer = raw_results.get("data", {}).get("viewer")
            if not isinstance(viewer, dict) or "repositories" not in viewer:
                raise GitHubQueryError(
                    "GraphQL response missing required viewer repository data"
                )

            maybe_name = viewer.get("name") or viewer.get("login") or maybe_name
            contrib_repos = viewer.get("repositoriesContributedTo", {})
            owned_repos = viewer.get("repositories", {})
            if not isinstance(owned_repos, dict) or not isinstance(
                contrib_repos, dict
            ):
                raise GitHubQueryError(
                    "GraphQL response has malformed repository data"
                )

            repos = owned_repos.get("nodes", [])
            if not self._ignore_forked_repos:
                repos += contrib_repos.get("nodes", [])

            for repo in repos:
                if repo is None:
                    continue
                name = repo.get("nameWithOwner")
                if name in repos_set or name in self._exclude_repos:
                    continue
                repos_set.add(name)
                stargazers += repo.get("stargazers").get("totalCount", 0)
                forks += repo.get("forkCount", 0)

                for lang in repo.get("languages", {}).get("edges", []):
                    language_name = lang.get("node", {}).get("name", "Other")
                    if language_name.lower() in exclude_langs_lower:
                        continue
                    if language_name in languages:
                        languages[language_name]["size"] += lang.get("size", 0)
                        languages[language_name]["occurrences"] += 1
                    else:
                        languages[language_name] = {
                            "size": lang.get("size", 0),
                            "occurrences": 1,
                            "color": lang.get("node", {}).get("color"),
                        }

            if owned_repos.get("pageInfo", {}).get(
                "hasNextPage", False
            ) or contrib_repos.get("pageInfo", {}).get("hasNextPage", False):
                next_owned = owned_repos.get("pageInfo", {}).get(
                    "endCursor", next_owned
                )
                next_contrib = contrib_repos.get("pageInfo", {}).get(
                    "endCursor", next_contrib
                )
            else:
                break

        # TODO: Improve languages to scale by number of contributions to
        #       specific filetypes
        langs_total = sum([v.get("size", 0) for v in languages.values()])
        if langs_total > 0:
            for k, v in languages.items():
                v["prop"] = 100 * (v.get("size", 0) / langs_total)

        self._name = maybe_name or "No Name"
        self._stargazers = stargazers
        self._forks = forks
        self._languages = languages
        self._repos = repos_set

    @property
    async def name(self) -> str:
        """
        :return: GitHub user's name (e.g., Jacob Strieb)
        """
        if self._name is not None:
            return self._name
        await self.get_stats()
        assert self._name is not None
        return self._name

    @property
    async def stargazers(self) -> int:
        """
        :return: total number of stargazers on user's repos
        """
        if self._stargazers is not None:
            return self._stargazers
        await self.get_stats()
        assert self._stargazers is not None
        return self._stargazers

    @property
    async def forks(self) -> int:
        """
        :return: total number of forks on user's repos
        """
        if self._forks is not None:
            return self._forks
        await self.get_stats()
        assert self._forks is not None
        return self._forks

    @property
    async def languages(self) -> Dict:
        """
        :return: summary of languages used by the user
        """
        if self._languages is not None:
            return self._languages
        await self.get_stats()
        assert self._languages is not None
        return self._languages

    @property
    async def languages_proportional(self) -> Dict:
        """
        :return: summary of languages used by the user, with proportional usage
        """
        if self._languages is None:
            await self.get_stats()
            assert self._languages is not None

        return {k: v.get("prop", 0) for (k, v) in self._languages.items()}

    @property
    async def repos(self) -> Set[str]:
        """
        :return: list of names of user's repos
        """
        if self._repos is not None:
            return self._repos
        await self.get_stats()
        assert self._repos is not None
        return self._repos

    @property
    async def total_contributions(self) -> int:
        """
        :return: count of user's total contributions as defined by GitHub
        """
        if self._total_contributions is not None:
            return self._total_contributions

        self._total_contributions = 0
        years = (
            (await self.queries.query(Queries.contrib_years()))
            .get("data", {})
            .get("viewer", {})
            .get("contributionsCollection", {})
            .get("contributionYears", [])
        )
        by_year = (
            (await self.queries.query(Queries.all_contribs(years)))
            .get("data", {})
            .get("viewer", {})
            .values()
        )
        for year in by_year:
            self._total_contributions += year.get("contributionCalendar", {}).get(
                "totalContributions", 0
            )
        return cast(int, self._total_contributions)

    @property
    async def lines_changed(self) -> Tuple[int, int]:
        """
        :return: count of total lines added, removed, or modified by the user
        """
        if self._lines_changed is not None:
            return self._lines_changed
        contributor_stats_wait_seconds = _env_float(
            "CONTRIBUTOR_STATS_WAIT_SECONDS", 8.0
        )
        repo_stats = await asyncio.gather(
            *[
                self.queries.query_rest(
                    f"/repos/{repo}/stats/contributors",
                    max_wait_seconds=contributor_stats_wait_seconds,
                )
                for repo in await self.repos
            ]
        )

        additions = 0
        deletions = 0
        for repo_result in repo_stats:
            for author_obj in repo_result:
                # Handle malformed response from the API by skipping this repo
                if not isinstance(author_obj, dict) or not isinstance(
                    author_obj.get("author", {}), dict
                ):
                    continue
                author = author_obj.get("author", {}).get("login", "")
                if author != self.username:
                    continue

                for week in author_obj.get("weeks", []):
                    additions += week.get("a", 0)
                    deletions += week.get("d", 0)

        self._lines_changed = (additions, deletions)
        return self._lines_changed

    @property
    async def views(self) -> int:
        """
        Note: only returns views for the last 14 days (as-per GitHub API)
        :return: total number of page views the user's projects have received
        """
        if self._views is not None:
            return self._views

        total = 0
        for repo in await self.repos:
            r = await self.queries.query_rest(f"/repos/{repo}/traffic/views")
            for view in r.get("views", []):
                total += view.get("count", 0)

        self._views = total
        return total


###############################################################################
# Main Function
###############################################################################


async def main() -> None:
    """
    Used mostly for testing; this module is not usually run standalone
    """
    access_token = os.getenv("ACCESS_TOKEN")
    user = os.getenv("GITHUB_ACTOR")
    if access_token is None or user is None:
        raise RuntimeError(
            "ACCESS_TOKEN and GITHUB_ACTOR environment variables cannot be None!"
        )
    async with aiohttp.ClientSession() as session:
        s = Stats(user, access_token, session)
        print(await s.to_str())


if __name__ == "__main__":
    asyncio.run(main())
