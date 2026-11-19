"""Resilient GitHub HTTP client enforcing read/write separation and real failure handling."""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any, Optional
import httpx

from mcp_work_triage.config import SecurityGovernanceError, Settings
from mcp_work_triage.models import IssueComment, IssueSummary, IssueThread


class RateLimitExceededError(Exception):
    """Raised when GitHub API rate limits are exceeded and retries are exhausted."""

    def __init__(self, message: str, retry_after: float = 0.0, reset_time: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after
        self.reset_time = reset_time


class GitHubAPIError(Exception):
    """Raised when GitHub API returns an unhandled error response."""

    def __init__(self, status_code: int, message: str, response_data: Any = None):
        super().__init__(f"GitHub API error {status_code}: {message}")
        self.status_code = status_code
        self.response_data = response_data


# Regex to strip private/internal markers
PRIVATE_MARKER_PATTERNS = [
    re.compile(r"<!--\s*(?:private|internal).*?-->", re.IGNORECASE | re.DOTALL),
    re.compile(r"\[(?:INTERNAL|PRIVATE)\].*?\[/(?:INTERNAL|PRIVATE)\]", re.IGNORECASE | re.DOTALL),
]


def sanitize_text(text: Optional[str], strip_markers: bool = True) -> str:
    """Strip private/internal markers from text if configured."""
    if not text:
        return ""
    if not strip_markers:
        return text

    sanitized = text
    for pattern in PRIVATE_MARKER_PATTERNS:
        sanitized = pattern.sub("", sanitized)
    return sanitized.strip()


def parse_next_link(link_header: Optional[str]) -> Optional[str]:
    """Extract URL for rel='next' from GitHub Link header."""
    if not link_header:
        return None
    links = link_header.split(",")
    for link in links:
        parts = link.strip().split(";")
        if len(parts) >= 2:
            url_part = parts[0].strip()
            rel_part = parts[1].strip()
            if 'rel="next"' in rel_part:
                # Strip '<' and '>'
                if url_part.startswith("<") and url_part.endswith(">"):
                    return url_part[1:-1]
    return None


class BaseResilientClient:
    """Base client with exponential backoff, jitter, timeouts, and rate limit handling."""

    def __init__(
        self,
        token: Optional[str] = None,
        base_url: str = "https://api.github.com",
        connect_timeout: float = 5.0,
        read_timeout: float = 15.0,
        max_retries: int = 3,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_retries = max_retries
        self._custom_client = http_client

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "mcp-work-triage/1.0",
        }
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        return headers

    def _create_client(self) -> httpx.AsyncClient:
        if self._custom_client is not None:
            return self._custom_client
        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.read_timeout,
            pool=self.connect_timeout,
        )
        return httpx.AsyncClient(headers=self._get_headers(), timeout=timeout)

    async def _send_with_retry(
        self,
        method: str,
        url: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[Any] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> httpx.Response:
        """Execute HTTP request with jittered exponential backoff for 429, 403 rate limits, and 5xx."""
        combined_headers = self._get_headers()
        if headers:
            combined_headers.update(headers)

        full_url = url if url.startswith("http") else f"{self.base_url}{url}"

        last_exception: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                # Use shared custom client or ephemeral client
                if self._custom_client is not None:
                    client = self._custom_client
                    response = await client.request(
                        method=method,
                        url=full_url,
                        params=params,
                        json=json_data,
                        headers=combined_headers,
                    )
                else:
                    async with self._create_client() as client:
                        response = await client.request(
                            method=method,
                            url=full_url,
                            params=params,
                            json=json_data,
                            headers=combined_headers,
                        )

                # Check Rate Limiting
                # GitHub returns 429 Too Many Requests OR 403 with X-RateLimit-Remaining: 0
                is_rate_limited = (
                    response.status_code == 429
                    or (
                        response.status_code == 403
                        and response.headers.get("X-RateLimit-Remaining") == "0"
                    )
                )

                if is_rate_limited:
                    retry_after = 0.0
                    if "Retry-After" in response.headers:
                        try:
                            retry_after = float(response.headers["Retry-After"])
                        except ValueError:
                            retry_after = 2.0
                    elif "X-RateLimit-Reset" in response.headers:
                        try:
                            reset_ts = int(response.headers["X-RateLimit-Reset"])
                            retry_after = max(0.5, float(reset_ts - time.time()))
                        except ValueError:
                            retry_after = 2.0
                    else:
                        retry_after = 1.0 * (2 ** attempt)

                    if attempt >= self.max_retries:
                        raise RateLimitExceededError(
                            f"GitHub rate limit exceeded. Max retries ({self.max_retries}) exhausted.",
                            retry_after=retry_after,
                            reset_time=int(response.headers.get("X-RateLimit-Reset", 0)) or None,
                        )

                    # Backoff sleep with jitter
                    sleep_time = min(30.0, retry_after + random.uniform(0.05, 0.25))
                    await asyncio.sleep(sleep_time)
                    continue

                # Check 5xx Server Errors (retriable)
                if response.status_code in (500, 502, 503, 504):
                    if attempt >= self.max_retries:
                        raise GitHubAPIError(
                            status_code=response.status_code,
                            message=f"GitHub server error {response.status_code} after {self.max_retries} retries",
                            response_data=response.text,
                        )
                    sleep_time = (0.5 * (2 ** attempt)) + random.uniform(0.05, 0.2)
                    await asyncio.sleep(sleep_time)
                    continue

                # Non-retriable 4xx errors
                if 400 <= response.status_code < 500:
                    raise GitHubAPIError(
                        status_code=response.status_code,
                        message=response.text,
                        response_data=response.text,
                    )

                return response

            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as exc:
                last_exception = exc
                if attempt >= self.max_retries:
                    raise exc
                sleep_time = (0.5 * (2 ** attempt)) + random.uniform(0.05, 0.2)
                await asyncio.sleep(sleep_time)

        if last_exception:
            raise last_exception
        raise RuntimeError("Unexpected retry loop termination")


class GitHubReadClient(BaseResilientClient):
    """Client strictly scoped to read-only operations.
    
    Contains NO write methods and has no access to write credentials.
    """

    def __init__(
        self,
        read_token: Optional[str] = None,
        base_url: str = "https://api.github.com",
        connect_timeout: float = 5.0,
        read_timeout: float = 15.0,
        max_retries: int = 3,
        max_pages: int = 10,
        strip_markers: bool = True,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        super().__init__(
            token=read_token,
            base_url=base_url,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            max_retries=max_retries,
            http_client=http_client,
        )
        self.max_pages = max_pages
        self.strip_markers = strip_markers

    async def list_issues(
        self,
        repo: str,
        state: str = "open",
        labels: Optional[list[str]] = None,
        per_page: int = 30,
        max_pages: Optional[int] = None,
    ) -> list[IssueSummary]:
        """Fetch issues with pagination drift protection and hard max_pages cap."""
        cap = max_pages if max_pages is not None else self.max_pages
        params: dict[str, Any] = {"state": state, "per_page": min(per_page, 100)}
        if labels:
            params["labels"] = ",".join(labels)

        current_url: Optional[str] = f"/repos/{repo}/issues"
        issues: list[IssueSummary] = []
        pages_fetched = 0
        visited_urls: set[str] = set()

        while current_url and pages_fetched < cap:
            full_url = current_url if current_url.startswith("http") else f"{self.base_url}{current_url}"
            if full_url in visited_urls:
                # Cycle detected in Link header
                break
            visited_urls.add(full_url)

            # Only pass params on the initial relative path
            req_params = params if pages_fetched == 0 else None
            response = await self._send_with_retry("GET", current_url, params=req_params)
            data = response.json()

            if isinstance(data, list):
                for item in data:
                    # Ignore PRs (GitHub issues endpoint includes PRs if 'pull_request' key exists)
                    if "pull_request" in item:
                        continue
                    label_names = [
                        lbl["name"] if isinstance(lbl, dict) else str(lbl)
                        for lbl in item.get("labels", [])
                    ]
                    issues.append(
                        IssueSummary(
                            number=item["number"],
                            title=item["title"],
                            state=item.get("state", "open"),
                            labels=label_names,
                            author=item.get("user", {}).get("login", "unknown"),
                            created_at=item["created_at"],
                            comments_count=item.get("comments", 0),
                            url=item.get("html_url", ""),
                        )
                    )

            pages_fetched += 1
            # Check pagination drift via Link header
            current_url = parse_next_link(response.headers.get("Link"))

        return issues

    async def get_issue_thread(self, repo: str, issue_number: int) -> IssueThread:
        """Fetch full issue body and comments, sanitizing private markers."""
        # 1. Fetch issue metadata & body
        issue_resp = await self._send_with_retry("GET", f"/repos/{repo}/issues/{issue_number}")
        issue_data = issue_resp.json()

        sanitized_body = sanitize_text(issue_data.get("body", ""), self.strip_markers)
        label_names = [
            lbl["name"] if isinstance(lbl, dict) else str(lbl)
            for lbl in issue_data.get("labels", [])
        ]

        # 2. Fetch comments (with pagination cap)
        comments: list[IssueComment] = []
        comments_url: Optional[str] = f"/repos/{repo}/issues/{issue_number}/comments"
        pages_fetched = 0
        visited_comments_urls: set[str] = set()

        while comments_url and pages_fetched < self.max_pages:
            full_comments_url = (
                comments_url if comments_url.startswith("http") else f"{self.base_url}{comments_url}"
            )
            if full_comments_url in visited_comments_urls:
                break
            visited_comments_urls.add(full_comments_url)

            comments_resp = await self._send_with_retry(
                "GET", comments_url, params={"per_page": 100} if pages_fetched == 0 else None
            )
            c_data = comments_resp.json()
            if isinstance(c_data, list):
                for c in c_data:
                    c_body = sanitize_text(c.get("body", ""), self.strip_markers)
                    comments.append(
                        IssueComment(
                            id=c["id"],
                            author=c.get("user", {}).get("login", "unknown"),
                            body=c_body,
                            created_at=c["created_at"],
                        )
                    )
            pages_fetched += 1
            comments_url = parse_next_link(comments_resp.headers.get("Link"))

        return IssueThread(
            number=issue_data["number"],
            title=issue_data["title"],
            body=sanitized_body,
            author=issue_data.get("user", {}).get("login", "unknown"),
            state=issue_data.get("state", "open"),
            labels=label_names,
            created_at=issue_data["created_at"],
            comments=comments,
            url=issue_data.get("html_url", ""),
        )


class GitHubWriteClient(BaseResilientClient):
    """Client for applying approved triage operations ONLY.
    
    Structurally lacks any code path for mutating assignees, milestones, due dates, or closing.
    """

    def __init__(
        self,
        write_token: str,
        base_url: str = "https://api.github.com",
        connect_timeout: float = 5.0,
        read_timeout: float = 15.0,
        max_retries: int = 3,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        if not write_token:
            raise SecurityGovernanceError("Write client requires a valid write token.")
        super().__init__(
            token=write_token,
            base_url=base_url,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            max_retries=max_retries,
            http_client=http_client,
        )
        self._committed_idempotency_keys: set[str] = set()

    async def apply_labels(
        self,
        repo: str,
        issue_number: int,
        labels: list[str],
        idempotency_key: str,
    ) -> list[str]:
        """Add triage labels to an issue. Idempotent by design."""
        if not labels:
            return []

        # POST /repos/{owner}/{repo}/issues/{issue_number}/labels
        url = f"/repos/{repo}/issues/{issue_number}/labels"
        response = await self._send_with_retry(
            method="POST",
            url=url,
            json_data={"labels": labels},
            headers={"X-Idempotency-Key": idempotency_key},
        )
        data = response.json()
        if isinstance(data, list):
            return [lbl["name"] if isinstance(lbl, dict) else str(lbl) for lbl in data]
        return labels

    async def add_comment(
        self,
        repo: str,
        issue_number: int,
        body: str,
        idempotency_key: str,
    ) -> Optional[int]:
        """Add an approved comment to an issue with idempotency protection."""
        if not body.strip():
            return None

        # Check local client idempotency guard
        if idempotency_key in self._committed_idempotency_keys:
            # Already submitted in this session
            return None

        url = f"/repos/{repo}/issues/{issue_number}/comments"
        response = await self._send_with_retry(
            method="POST",
            url=url,
            json_data={"body": body},
            headers={"X-Idempotency-Key": idempotency_key},
        )
        self._committed_idempotency_keys.add(idempotency_key)
        data = response.json()
        return data.get("id") if isinstance(data, dict) else None
