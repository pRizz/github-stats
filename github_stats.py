#!/usr/bin/python3

import asyncio
import datetime as dt
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Set, Any, cast

import aiohttp
import requests


def _empty_rest_value(normalized_path: str) -> Any:
    """JSON shape GitHub would return for an empty successful response."""
    if normalized_path.endswith("/commits"):
        return []
    return {}


@dataclass(frozen=True)
class _StatusDecision:
    category: str
    retryable: bool
    message: str


@dataclass(frozen=True)
class ApiDegradation:
    repo: str
    endpoint: str
    status: int
    category: str
    message: str
    attempts: int = 1
    retry_count: int = 0
    wait_seconds: float = 0.0
    elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class ApiWaitStat:
    repo: str
    endpoint: str
    status: int
    category: str
    degraded: bool
    attempts: int
    retry_count: int
    wait_seconds: float
    elapsed_seconds: float
    message: str


@dataclass
class StatsReport:
    total_repos: int = 0
    traffic_degraded: List[ApiDegradation] = field(default_factory=list)
    monthly_commits_degraded: List[ApiDegradation] = field(default_factory=list)
    critical_failures: List[str] = field(default_factory=list)
    rest_requests_total: int = 0
    rest_retries_total: int = 0
    rest_wait_seconds_total: float = 0.0
    traffic_wait_seconds_total: float = 0.0
    monthly_commits_wait_seconds_total: float = 0.0
    slowest_requests: List[ApiWaitStat] = field(default_factory=list)

    def record_rest_call(self, item: ApiWaitStat, limit: int = 10) -> None:
        self.rest_requests_total += 1
        self.rest_retries_total += item.retry_count
        self.rest_wait_seconds_total += item.wait_seconds
        if item.endpoint.startswith("traffic/"):
            self.traffic_wait_seconds_total += item.wait_seconds
        elif item.endpoint == "commits":
            self.monthly_commits_wait_seconds_total += item.wait_seconds

        self.slowest_requests.append(item)
        self.slowest_requests.sort(
            key=lambda stat: (stat.wait_seconds, stat.elapsed_seconds),
            reverse=True,
        )
        del self.slowest_requests[limit:]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_repos": self.total_repos,
            "traffic_degraded": [asdict(item) for item in self.traffic_degraded],
            "monthly_commits_degraded": [
                asdict(item) for item in self.monthly_commits_degraded
            ],
            "critical_failures": self.critical_failures,
            "rest_requests_total": self.rest_requests_total,
            "rest_retries_total": self.rest_retries_total,
            "rest_wait_seconds_total": self.rest_wait_seconds_total,
            "traffic_wait_seconds_total": self.traffic_wait_seconds_total,
            "monthly_commits_wait_seconds_total": (
                self.monthly_commits_wait_seconds_total
            ),
            "slowest_requests": [
                asdict(item) for item in self.slowest_requests
            ],
        }


@dataclass(frozen=True)
class MonthWindow:
    key: str
    label: str
    since: str
    until: str
    is_current: bool = False


@dataclass(frozen=True)
class MonthlyCommitRecord:
    key: str
    label: str
    count: int
    is_current: bool


@dataclass(frozen=True)
class MonthlyCommitScanResult:
    counts: Dict[str, int]
    repo_count: int
    scanned_months: int
    identity_patterns: List[str]
    degraded_months: Set[str] = field(default_factory=set)


@dataclass(frozen=True)
class RepoMetric:
    name_with_owner: str
    display_name: str
    is_private: bool
    is_fork: bool
    is_archived: bool
    stargazers: int
    forks: int
    updated_at: str
    pushed_at: str
    open_issues: int
    open_pull_requests: int
    releases: int
    tags: int
    languages: Dict[str, Dict[str, Any]]

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "display_name": self.display_name,
            "is_private": self.is_private,
            "is_fork": self.is_fork,
            "is_archived": self.is_archived,
            "stargazers": self.stargazers,
            "forks": self.forks,
            "updated_at": self.updated_at,
            "pushed_at": self.pushed_at,
            "open_issues": self.open_issues,
            "open_pull_requests": self.open_pull_requests,
            "releases": self.releases,
            "tags": self.tags,
            "languages": self.languages,
        }


@dataclass(frozen=True)
class ContributionBreakdown:
    commits: int = 0
    issues: int = 0
    pull_requests: int = 0
    pull_request_reviews: int = 0
    repositories: int = 0
    restricted: int = 0

    @property
    def total(self) -> int:
        return (
            self.commits
            + self.issues
            + self.pull_requests
            + self.pull_request_reviews
            + self.repositories
            + self.restricted
        )

    def to_dict(self) -> Dict[str, int]:
        return {
            "commits": self.commits,
            "issues": self.issues,
            "pull_requests": self.pull_requests,
            "pull_request_reviews": self.pull_request_reviews,
            "repositories": self.repositories,
            "restricted": self.restricted,
            "total": self.total,
        }


@dataclass(frozen=True)
class RestQueryResult:
    payload: Any
    degraded: bool = False
    status: int = 0
    category: str = "success"
    message: str = ""
    attempts: int = 1
    retry_count: int = 0
    wait_seconds: float = 0.0
    elapsed_seconds: float = 0.0


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
    if status == 401:
        return _StatusDecision(
            "unauthenticated", False, "missing, invalid, or expired token"
        )
    if status == 403:
        return _StatusDecision(
            "auth_or_permission_error", False, "authentication or permission failure"
        )
    if status in {404, 410}:
        return _StatusDecision(
            "not_found_or_gone", False, "resource not found or gone"
        )
    if status == 409:
        return _StatusDecision("conflict", False, "resource conflict")
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
    if "traffic/views" in normalized_path or "traffic/clones" in normalized_path:
        return decision.category in {
            "auth_or_permission_error",
            "not_found_or_gone",
        }
    if normalized_path.endswith("/commits"):
        return decision.category in {
            "auth_or_permission_error",
            "conflict",
            "not_found_or_gone",
            "rate_limited",
        }
    return False


def _can_degrade_rest_body(normalized_path: str) -> bool:
    return (
        "traffic/views" in normalized_path
        or "traffic/clones" in normalized_path
        or normalized_path.endswith("/commits")
    )


def _degraded_rest_result(
    normalized_path: str,
    status: int,
    decision: _StatusDecision,
    body_preview: str = "",
    attempts: int = 1,
    retry_count: int = 0,
    wait_seconds: float = 0.0,
    started_at: Optional[float] = None,
) -> RestQueryResult:
    elapsed_seconds = 0.0 if started_at is None else time.monotonic() - started_at
    return RestQueryResult(
        payload=_empty_rest_value(normalized_path),
        degraded=True,
        status=status,
        category=decision.category,
        message=_status_error_message("REST", status, decision, body_preview),
        attempts=attempts,
        retry_count=retry_count,
        wait_seconds=wait_seconds,
        elapsed_seconds=elapsed_seconds,
    )


def _success_rest_result(
    payload: Any,
    status: int,
    decision: _StatusDecision,
    attempts: int = 1,
    retry_count: int = 0,
    wait_seconds: float = 0.0,
    started_at: Optional[float] = None,
) -> RestQueryResult:
    elapsed_seconds = 0.0 if started_at is None else time.monotonic() - started_at
    return RestQueryResult(
        payload=payload,
        degraded=False,
        status=status,
        category=decision.category,
        message=decision.message,
        attempts=attempts,
        retry_count=retry_count,
        wait_seconds=wait_seconds,
        elapsed_seconds=elapsed_seconds,
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


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        print(f"Invalid {name} value {raw_value!r}; using {default}.")
        return default


def _current_utc_year() -> int:
    return dt.datetime.now(dt.timezone.utc).year


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _add_months(value: dt.datetime, months: int) -> dt.datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return value.replace(year=year, month=month)


def _parse_github_datetime(value: str) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _format_github_datetime(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def rolling_month_windows(
    now: Optional[dt.datetime] = None,
    month_count: int = 13,
) -> List[MonthWindow]:
    current_time = _utc_now() if now is None else now.astimezone(dt.timezone.utc)
    current_start = current_time.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    first_start = _add_months(current_start, -(month_count - 1))
    windows = []

    for index in range(month_count):
        start = _add_months(first_start, index)
        next_start = _add_months(start, 1)
        is_current = start.year == current_start.year and start.month == current_start.month
        until = current_time if is_current else next_start
        windows.append(
            MonthWindow(
                key=start.strftime("%Y-%m"),
                label=start.strftime("%b '%y"),
                since=_format_github_datetime(start),
                until=_format_github_datetime(until),
                is_current=is_current,
            )
        )

    return windows


def monthly_commit_identity_patterns(username: Optional[str] = None) -> List[str]:
    raw_patterns = os.getenv("MONTHLY_COMMIT_IDENTITY_PATTERNS")
    if raw_patterns:
        candidates = [item.strip() for item in raw_patterns.split(",")]
    else:
        candidates = ["Peter Ryszkiewicz", "prizz", "pRizz"]
        if username:
            candidates.append(username)

    patterns = []
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        patterns.append(candidate)
    return patterns


def _field_matches(value: Any, patterns: List[str]) -> bool:
    if not isinstance(value, str) or not value:
        return False
    lowered = value.casefold()
    return any(pattern.casefold() in lowered for pattern in patterns)


def _commit_identity_match_date(
    commit: Dict[str, Any],
    patterns: List[str],
) -> Optional[dt.datetime]:
    commit_data = commit.get("commit", {})
    if not isinstance(commit_data, dict):
        return None

    author_data = commit_data.get("author", {})
    committer_data = commit_data.get("committer", {})
    rest_author = commit.get("author", {})
    rest_committer = commit.get("committer", {})
    if rest_author is None:
        rest_author = {}
    if rest_committer is None:
        rest_committer = {}

    author_fields = []
    committer_fields = []
    if isinstance(author_data, dict):
        author_fields.extend([author_data.get("name"), author_data.get("email")])
    if isinstance(rest_author, dict):
        author_fields.append(rest_author.get("login"))
    if isinstance(committer_data, dict):
        committer_fields.extend(
            [committer_data.get("name"), committer_data.get("email")]
        )
    if isinstance(rest_committer, dict):
        committer_fields.append(rest_committer.get("login"))

    if any(_field_matches(field, patterns) for field in author_fields):
        if isinstance(author_data, dict):
            return _parse_github_datetime(str(author_data.get("date", "")))
        return None
    if any(_field_matches(field, patterns) for field in committer_fields):
        if isinstance(committer_data, dict):
            return _parse_github_datetime(str(committer_data.get("date", "")))
    return None


class _AsyncRequestThrottle:
    def __init__(self, delay_seconds: float, max_concurrent: int):
        self.delay_seconds = max(0.0, delay_seconds)
        self.semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self.lock = asyncio.Lock()
        self.next_request_at = 0.0

    async def run(self, request_factory: Any) -> Any:
        async with self.semaphore:
            async with self.lock:
                now = time.monotonic()
                wait_seconds = self.next_request_at - now
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                self.next_request_at = time.monotonic() + self.delay_seconds
            return await request_factory()


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
        result = await self.query_rest_with_outcome(path, params, max_wait_seconds)
        return result.payload

    async def query_rest_with_outcome(
        self,
        path: str,
        params: Optional[Dict] = None,
        max_wait_seconds: float = 90.0,
    ) -> RestQueryResult:
        """
        Make a request to the REST API
        :param path: API path to query
        :param params: Query parameters to be passed to the API
        :param max_wait_seconds: Maximum time to wait on retryable responses
        :return: REST payload plus status/degradation metadata
        """

        if params is None:
            params = dict()
        normalized = path[1:] if path.startswith("/") else path

        # Without a wall-clock cap, retryable responses can run for hours in CI.
        started_at = time.monotonic()
        deadline = time.monotonic() + max_wait_seconds
        max_iterations = 35
        last_status = 0
        last_decision = _StatusDecision("unknown_status", False, "no response yet")
        last_preview = ""
        retry_count = 0
        wait_seconds = 0.0

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
                    return _degraded_rest_result(
                        normalized,
                        last_status,
                        last_decision,
                        last_preview,
                        attempts=attempt - 1,
                        retry_count=retry_count,
                        wait_seconds=wait_seconds,
                        started_at=started_at,
                    )
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
                            return _degraded_rest_result(
                                normalized,
                                r_async.status,
                                decision,
                                preview,
                                attempts=attempt,
                                retry_count=retry_count,
                                wait_seconds=wait_seconds,
                                started_at=started_at,
                            )
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
                    retry_count += 1
                    wait_seconds += delay
                    await asyncio.sleep(delay)
                    continue

                if _is_success_empty(decision):
                    return _success_rest_result(
                        _empty_rest_value(normalized),
                        r_async.status,
                        decision,
                        attempts=attempt,
                        retry_count=retry_count,
                        wait_seconds=wait_seconds,
                        started_at=started_at,
                    )
                if not _is_success_with_json(decision):
                    if _can_degrade_rest_status(normalized, decision):
                        print(
                            f"{normalized} returned {r_async.status} "
                            f"({decision.category}); using empty endpoint data."
                        )
                        return _degraded_rest_result(
                            normalized,
                            r_async.status,
                            decision,
                            preview,
                            attempts=attempt,
                            retry_count=retry_count,
                            wait_seconds=wait_seconds,
                            started_at=started_at,
                        )
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
                        return _degraded_rest_result(
                            normalized,
                            r_async.status,
                            decision,
                            preview,
                            attempts=attempt,
                            retry_count=retry_count,
                            wait_seconds=wait_seconds,
                            started_at=started_at,
                        )
                    raise GitHubQueryError(
                        f"{normalized} returned non-JSON: {error}; {preview}"
                    ) from error
                if result is not None:
                    return _success_rest_result(
                        result,
                        r_async.status,
                        decision,
                        attempts=attempt,
                        retry_count=retry_count,
                        wait_seconds=wait_seconds,
                        started_at=started_at,
                    )
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
                            return _degraded_rest_result(
                                normalized,
                                r_requests.status_code,
                                decision,
                                preview,
                                attempts=attempt,
                                retry_count=retry_count,
                                wait_seconds=wait_seconds,
                                started_at=started_at,
                            )
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
                    retry_count += 1
                    wait_seconds += delay
                    await asyncio.sleep(delay)
                    continue

                if _is_success_empty(decision):
                    return _success_rest_result(
                        _empty_rest_value(normalized),
                        r_requests.status_code,
                        decision,
                        attempts=attempt,
                        retry_count=retry_count,
                        wait_seconds=wait_seconds,
                        started_at=started_at,
                    )
                if not _is_success_with_json(decision):
                    if _can_degrade_rest_status(normalized, decision):
                        print(
                            f"{normalized} returned {r_requests.status_code} "
                            f"via fallback ({decision.category}); "
                            "using empty endpoint data."
                        )
                        return _degraded_rest_result(
                            normalized,
                            r_requests.status_code,
                            decision,
                            preview,
                            attempts=attempt,
                            retry_count=retry_count,
                            wait_seconds=wait_seconds,
                            started_at=started_at,
                        )
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
                        return _degraded_rest_result(
                            normalized,
                            r_requests.status_code,
                            decision,
                            preview,
                            attempts=attempt,
                            retry_count=retry_count,
                            wait_seconds=wait_seconds,
                            started_at=started_at,
                        )
                    raise GitHubQueryError(
                        f"{normalized} fallback returned non-JSON: "
                        f"{error}; {preview}"
                    ) from error
                if result is not None:
                    return _success_rest_result(
                        result,
                        r_requests.status_code,
                        decision,
                        attempts=attempt,
                        retry_count=retry_count,
                        wait_seconds=wait_seconds,
                        started_at=started_at,
                    )
            except GitHubQueryError:
                raise
            except Exception as error:
                print(f"requests failed for rest query: {error}")

        if _can_degrade_rest_status(normalized, last_decision):
            print(
                f"query_rest: too many retryable responses for {normalized}; "
                "data for this endpoint may be incomplete."
            )
            return _degraded_rest_result(
                normalized,
                last_status,
                last_decision,
                last_preview,
                attempts=max_iterations,
                retry_count=retry_count,
                wait_seconds=wait_seconds,
                started_at=started_at,
            )
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
        isPrivate
        isFork
        isArchived
        updatedAt
        pushedAt
        stargazers {{
          totalCount
        }}
        forkCount
        issues(states: OPEN) {{
          totalCount
        }}
        pullRequests(states: OPEN) {{
          totalCount
        }}
        refs(refPrefix: "refs/tags/", first: 1) {{
          totalCount
        }}
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
        isPrivate
        isFork
        isArchived
        updatedAt
        pushedAt
        stargazers {{
          totalCount
        }}
        forkCount
        issues(states: OPEN) {{
          totalCount
        }}
        pullRequests(states: OPEN) {{
          totalCount
        }}
        refs(refPrefix: "refs/tags/", first: 1) {{
          totalCount
        }}
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
    def owned_repos_overview(owned_cursor: Optional[str] = None) -> str:
        """
        :return: GraphQL query with overview of repositories owned by the user
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
        isPrivate
        isFork
        isArchived
        updatedAt
        pushedAt
        stargazers {{
          totalCount
        }}
        forkCount
        issues(states: OPEN) {{
          totalCount
        }}
        pullRequests(states: OPEN) {{
          totalCount
        }}
        refs(refPrefix: "refs/tags/", first: 1) {{
          totalCount
        }}
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
    def contributed_repos_overview(contrib_cursor: Optional[str] = None) -> str:
        """
        :return: GraphQL query with overview of repositories the user contributed to
        """
        return f"""{{
  viewer {{
    login,
    name,
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
        isPrivate
        isFork
        isArchived
        updatedAt
        pushedAt
        stargazers {{
          totalCount
        }}
        forkCount
        issues(states: OPEN) {{
          totalCount
        }}
        pullRequests(states: OPEN) {{
          totalCount
        }}
        refs(refPrefix: "refs/tags/", first: 1) {{
          totalCount
        }}
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
    def current_year_contribs(year: int) -> str:
        """
        :param year: year to query for
        :return: query to retrieve contribution information for the given year
        """
        return f"""
query {{
  viewer {{
    contributionsCollection(
        from: "{year}-01-01T00:00:00Z",
        to: "{year + 1}-01-01T00:00:00Z"
    ) {{
      contributionCalendar {{
        totalContributions
      }}
    }}
  }}
}}
"""

    @staticmethod
    def merged_pull_requests(username: str) -> str:
        """
        :param username: GitHub username to search for
        :return: query to count merged pull requests authored by the user
        """
        return f"""
query {{
  search(
      query: "author:{username} is:pr is:merged",
      type: ISSUE,
      first: 1
  ) {{
    issueCount
  }}
}}
"""

    @staticmethod
    def monthly_commit_audit(windows: List[MonthWindow]) -> str:
        by_month = "\n".join(
            f"""
    m{index}: contributionsCollection(
        from: "{window.since}",
        to: "{window.until}"
    ) {{
      totalCommitContributions
    }}
"""
            for index, window in enumerate(windows)
        )
        return f"""
query {{
  viewer {{
    {by_month}
  }}
}}
"""

    @staticmethod
    def contribution_breakdown(year: int) -> str:
        """
        :param year: year to query for
        :return: query to retrieve contribution-type totals for a year
        """
        return f"""
query {{
  viewer {{
    contributionsCollection(
        from: "{year}-01-01T00:00:00Z",
        to: "{year + 1}-01-01T00:00:00Z"
    ) {{
      totalCommitContributions
      totalIssueContributions
      totalPullRequestContributions
      totalPullRequestReviewContributions
      totalRepositoryContributions
      restrictedContributionsCount
    }}
  }}
}}
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
        self._current_year_contributions: Optional[int] = None
        self._merged_pull_requests: Optional[int] = None
        self._languages: Optional[Dict[str, Any]] = None
        self._repos: Optional[Set[str]] = None
        self._repo_metrics: Optional[Dict[str, RepoMetric]] = None
        self._views: Optional[int] = None
        self._clones: Optional[int] = None
        self._current_year_contribution_breakdown: Optional[
            ContributionBreakdown
        ] = None
        self.report = StatsReport()

    def _record_degradation(
        self,
        repo: str,
        endpoint: str,
        result: RestQueryResult,
    ) -> None:
        degradation = ApiDegradation(
            repo=repo,
            endpoint=endpoint,
            status=result.status,
            category=result.category,
            message=result.message,
            attempts=result.attempts,
            retry_count=result.retry_count,
            wait_seconds=result.wait_seconds,
            elapsed_seconds=result.elapsed_seconds,
        )
        if endpoint.startswith("traffic/"):
            self.report.traffic_degraded.append(degradation)
        elif endpoint == "commits":
            self.report.monthly_commits_degraded.append(degradation)

    def _record_rest_call(
        self,
        repo: str,
        endpoint: str,
        result: RestQueryResult,
    ) -> None:
        self.report.record_rest_call(
            ApiWaitStat(
                repo=repo,
                endpoint=endpoint,
                status=result.status,
                category=result.category,
                degraded=result.degraded,
                attempts=result.attempts,
                retry_count=result.retry_count,
                wait_seconds=result.wait_seconds,
                elapsed_seconds=result.elapsed_seconds,
                message=result.message,
            )
        )

    async def to_str(self) -> str:
        """
        :return: summary of all available statistics
        """
        languages = await self.languages_proportional
        formatted_languages = "\n  - ".join(
            [f"{k}: {v:0.4f}%" for k, v in languages.items()]
        )
        return f"""Name: {await self.name}
Stargazers: {await self.stargazers:,}
Forks: {await self.forks:,}
All-time contributions: {await self.total_contributions:,}
Current-year contributions: {await self.current_year_contributions:,}
Merged pull requests: {await self.merged_pull_requests:,}
Repositories with contributions: {len(await self.repos)}
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
        raw_repo_metrics: Dict[str, Dict[str, Any]] = {}
        maybe_name: Optional[str] = None

        exclude_langs_lower = {x.lower() for x in self._exclude_langs}

        def record_repo(repo: Any) -> None:
            nonlocal stargazers, forks
            if repo is None:
                return
            name = repo.get("nameWithOwner")
            if not isinstance(name, str):
                return
            if name in repos_set or name in self._exclude_repos:
                return
            repos_set.add(name)
            repo_stargazers = repo.get("stargazers", {}).get("totalCount", 0)
            repo_forks = repo.get("forkCount", 0)
            stargazers += repo_stargazers
            forks += repo.get("forkCount", 0)
            raw_repo_languages: Dict[str, Dict[str, Any]] = {}

            for lang in repo.get("languages", {}).get("edges", []):
                language_name = lang.get("node", {}).get("name", "Other")
                if language_name.lower() in exclude_langs_lower:
                    continue
                raw_repo_languages[language_name] = {
                    "size": lang.get("size", 0),
                    "color": lang.get("node", {}).get("color"),
                }
                if language_name in languages:
                    languages[language_name]["size"] += lang.get("size", 0)
                    languages[language_name]["occurrences"] += 1
                else:
                    languages[language_name] = {
                        "size": lang.get("size", 0),
                        "occurrences": 1,
                        "color": lang.get("node", {}).get("color"),
                    }
            raw_repo_metrics[name] = {
                "is_private": bool(repo.get("isPrivate", False)),
                "is_fork": bool(repo.get("isFork", False)),
                "is_archived": bool(repo.get("isArchived", False)),
                "stargazers": int(repo_stargazers),
                "forks": int(repo_forks),
                "updated_at": str(repo.get("updatedAt") or ""),
                "pushed_at": str(repo.get("pushedAt") or ""),
                "open_issues": int(repo.get("issues", {}).get("totalCount", 0)),
                "open_pull_requests": int(
                    repo.get("pullRequests", {}).get("totalCount", 0)
                ),
                "releases": int(repo.get("releases", {}).get("totalCount", 0)),
                "tags": int(repo.get("refs", {}).get("totalCount", 0)),
                "languages": raw_repo_languages,
            }

        next_owned = None
        while True:
            raw_results = await self.queries.query(
                Queries.owned_repos_overview(owned_cursor=next_owned)
            )
            viewer = raw_results.get("data", {}).get("viewer")
            if not isinstance(viewer, dict) or "repositories" not in viewer:
                raise GitHubQueryError(
                    "GraphQL response missing required owned repository data"
                )
            maybe_name = viewer.get("name") or viewer.get("login") or maybe_name
            owned_repos = viewer.get("repositories", {})
            if not isinstance(owned_repos, dict):
                raise GitHubQueryError(
                    "GraphQL response has malformed owned repository data"
                )
            for repo in owned_repos.get("nodes", []):
                record_repo(repo)

            if owned_repos.get("pageInfo", {}).get("hasNextPage", False):
                next_owned = owned_repos.get("pageInfo", {}).get(
                    "endCursor", next_owned
                )
            else:
                break

        next_contrib = None
        while not self._ignore_forked_repos:
            raw_results = await self.queries.query(
                Queries.contributed_repos_overview(contrib_cursor=next_contrib)
            )
            viewer = raw_results.get("data", {}).get("viewer")
            if not isinstance(viewer, dict) or "repositoriesContributedTo" not in viewer:
                raise GitHubQueryError(
                    "GraphQL response missing required contributed repository data"
                )
            maybe_name = viewer.get("name") or viewer.get("login") or maybe_name
            contrib_repos = viewer.get("repositoriesContributedTo", {})
            if not isinstance(contrib_repos, dict):
                raise GitHubQueryError(
                    "GraphQL response has malformed contributed repository data"
                )
            for repo in contrib_repos.get("nodes", []):
                record_repo(repo)

            if contrib_repos.get("pageInfo", {}).get("hasNextPage", False):
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
        private_labels = {
            name: f"Private repo #{index}"
            for index, name in enumerate(
                sorted(
                    name
                    for name, data in raw_repo_metrics.items()
                    if data["is_private"]
                ),
                start=1,
            )
        }
        self._repo_metrics = {
            name: RepoMetric(
                name_with_owner=name,
                display_name=private_labels.get(name, name),
                is_private=data["is_private"],
                is_fork=data["is_fork"],
                is_archived=data["is_archived"],
                stargazers=data["stargazers"],
                forks=data["forks"],
                updated_at=data["updated_at"],
                pushed_at=data["pushed_at"],
                open_issues=data["open_issues"],
                open_pull_requests=data["open_pull_requests"],
                releases=data["releases"],
                tags=data["tags"],
                languages=data["languages"],
            )
            for name, data in raw_repo_metrics.items()
        }
        self.report.total_repos = len(repos_set)

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
    async def repo_metrics(self) -> Dict[str, RepoMetric]:
        """
        :return: repository metadata with private repository display names redacted
        """
        if self._repo_metrics is not None:
            return self._repo_metrics
        await self.get_stats()
        assert self._repo_metrics is not None
        return self._repo_metrics

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
    async def current_year_contributions(self) -> int:
        """
        :return: count of the user's GitHub contributions in the current UTC year
        """
        if self._current_year_contributions is not None:
            return self._current_year_contributions

        year = _current_utc_year()
        result = (
            (await self.queries.query(Queries.current_year_contribs(year)))
            .get("data", {})
            .get("viewer", {})
            .get("contributionsCollection", {})
            .get("contributionCalendar", {})
            .get("totalContributions", 0)
        )
        self._current_year_contributions = int(result)
        return self._current_year_contributions

    @property
    async def current_year_contribution_breakdown(self) -> ContributionBreakdown:
        """
        :return: current-year contribution counts grouped by contribution type
        """
        if self._current_year_contribution_breakdown is not None:
            return self._current_year_contribution_breakdown

        year = _current_utc_year()
        result = (
            (await self.queries.query(Queries.contribution_breakdown(year)))
            .get("data", {})
            .get("viewer", {})
            .get("contributionsCollection", {})
        )
        if not isinstance(result, dict):
            result = {}
        self._current_year_contribution_breakdown = ContributionBreakdown(
            commits=int(result.get("totalCommitContributions", 0)),
            issues=int(result.get("totalIssueContributions", 0)),
            pull_requests=int(result.get("totalPullRequestContributions", 0)),
            pull_request_reviews=int(
                result.get("totalPullRequestReviewContributions", 0)
            ),
            repositories=int(result.get("totalRepositoryContributions", 0)),
            restricted=int(result.get("restrictedContributionsCount", 0)),
        )
        return self._current_year_contribution_breakdown

    @property
    async def merged_pull_requests(self) -> int:
        """
        :return: count of merged pull requests authored by the user
        """
        if self._merged_pull_requests is not None:
            return self._merged_pull_requests

        result = (
            (await self.queries.query(Queries.merged_pull_requests(self.username)))
            .get("data", {})
            .get("search", {})
            .get("issueCount", 0)
        )
        self._merged_pull_requests = int(result)
        return self._merged_pull_requests

    async def scan_monthly_commits(
        self,
        windows: List[MonthWindow],
        identity_patterns: Optional[List[str]] = None,
        extra_repos: Optional[Set[str]] = None,
    ) -> MonthlyCommitScanResult:
        """
        Count commits by month using raw commit metadata identity matching.
        """
        patterns = (
            monthly_commit_identity_patterns(self.username)
            if identity_patterns is None
            else identity_patterns
        )
        repos = set(await self.repos)
        if extra_repos is not None:
            repos.update(extra_repos)

        counts: Dict[str, int] = {window.key: 0 for window in windows}
        seen_by_month: Dict[str, Set[str]] = {window.key: set() for window in windows}
        degraded_months: Set[str] = set()
        max_wait_seconds = _env_float("MONTHLY_COMMITS_WAIT_SECONDS", 12.0)
        request_delay_seconds = _env_float(
            "MONTHLY_COMMITS_REQUEST_DELAY_SECONDS",
            0.75,
        )
        max_concurrent_requests = _env_int(
            "MONTHLY_COMMITS_MAX_CONCURRENT_REQUESTS",
            1,
        )
        commit_request_throttle = _AsyncRequestThrottle(
            request_delay_seconds,
            max_concurrent_requests,
        )

        async def scan_repo_window(repo: str, window: MonthWindow) -> None:
            page = 1
            while True:
                result = await commit_request_throttle.run(
                    lambda: self.queries.query_rest_with_outcome(
                        f"/repos/{repo}/commits",
                        params={
                            "since": window.since,
                            "until": window.until,
                            "per_page": "100",
                            "page": str(page),
                        },
                        max_wait_seconds=max_wait_seconds,
                    )
                )
                self._record_rest_call(repo, "commits", result)
                if result.degraded:
                    self._record_degradation(repo, "commits", result)
                    if result.category in {
                        "auth_or_permission_error",
                        "rate_limited",
                    }:
                        degraded_months.add(window.key)
                    return

                payload = result.payload if isinstance(result.payload, list) else []
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    match_date = _commit_identity_match_date(item, patterns)
                    if match_date is None or match_date.strftime("%Y-%m") != window.key:
                        continue
                    sha = item.get("sha")
                    if not isinstance(sha, str) or not sha:
                        continue
                    if sha in seen_by_month[window.key]:
                        continue
                    seen_by_month[window.key].add(sha)
                    counts[window.key] += 1

                if len(payload) < 100:
                    return
                page += 1

        await asyncio.gather(
            *[
                scan_repo_window(repo, window)
                for repo in sorted(repos)
                for window in windows
            ]
        )

        return MonthlyCommitScanResult(
            counts=counts,
            repo_count=len(repos),
            scanned_months=len(windows),
            identity_patterns=patterns,
            degraded_months=degraded_months,
        )

    async def audit_monthly_commit_counts(
        self,
        windows: List[MonthWindow],
    ) -> Dict[str, int]:
        """
        Return GitHub-attributed commit contribution counts for comparison only.
        """
        raw_results = await self.queries.query(Queries.monthly_commit_audit(windows))
        viewer = raw_results.get("data", {}).get("viewer", {})
        if not isinstance(viewer, dict):
            raise GitHubQueryError(
                "GraphQL response missing monthly commit audit data"
            )

        counts = {}
        for index, window in enumerate(windows):
            month_data = viewer.get(f"m{index}", {})
            if not isinstance(month_data, dict):
                counts[window.key] = 0
                continue
            counts[window.key] = int(month_data.get("totalCommitContributions", 0))
        return counts

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
            result = await self.queries.query_rest_with_outcome(
                f"/repos/{repo}/traffic/views"
            )
            self._record_rest_call(repo, "traffic/views", result)
            if result.degraded:
                self._record_degradation(repo, "traffic/views", result)
            for view in result.payload.get("views", []):
                total += view.get("count", 0)

        self._views = total
        return total

    @property
    async def clones(self) -> int:
        """
        Note: only returns clones for the last 14 days (as-per GitHub API)
        :return: total number of clones for the user's projects
        """
        if self._clones is not None:
            return self._clones

        total = 0
        for repo in await self.repos:
            result = await self.queries.query_rest_with_outcome(
                f"/repos/{repo}/traffic/clones"
            )
            self._record_rest_call(repo, "traffic/clones", result)
            if result.degraded:
                self._record_degradation(repo, "traffic/clones", result)
            for clone in result.payload.get("clones", []):
                total += clone.get("count", 0)

        self._clones = total
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
