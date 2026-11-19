"""Tests simulating real-world API failure modes: 429 backoff, 5xx retries, timeouts, and pagination."""

import asyncio
import time
import httpx
import pytest

from mcp_work_triage.github_client import (
    GitHubAPIError,
    GitHubReadClient,
    GitHubWriteClient,
    RateLimitExceededError,
    parse_next_link,
)


@pytest.mark.asyncio
async def test_rate_limit_429_retry_and_exhaustion():
    """Verify that 429 with Retry-After triggers retries and raises RateLimitExceededError upon exhaustion."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            status_code=429,
            headers={"Retry-After": "0.01"},
            json={"message": "Too Many Requests"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GitHubReadClient(
            read_token="mock_token",
            max_retries=2,
            http_client=http_client,
        )

        with pytest.raises(RateLimitExceededError) as exc_info:
            await client.list_issues(repo="owner/repo")

        # Initial try + 2 retries = 3 calls
        assert call_count == 3
        assert "rate limit exceeded" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_rate_limit_403_secondary_limit():
    """Verify that 403 with X-RateLimit-Remaining: 0 triggers backoff and handles reset."""
    call_count = 0
    reset_epoch = int(time.time()) + 1

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            status_code=403,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(reset_epoch),
            },
            json={"message": "API rate limit exceeded"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GitHubReadClient(
            read_token="mock_token",
            max_retries=1,
            http_client=http_client,
        )

        with pytest.raises(RateLimitExceededError):
            await client.list_issues(repo="owner/repo")

        assert call_count == 2


@pytest.mark.asyncio
async def test_transient_5xx_retry_and_recovery():
    """Verify that transient 502/503 errors retry and recover when subsequent call succeeds."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(status_code=502, text="Bad Gateway")
        return httpx.Response(
            status_code=200,
            json=[
                {
                    "number": 1,
                    "title": "Recovered Issue",
                    "user": {"login": "octocat"},
                    "created_at": "2026-11-17T12:00:00Z",
                    "comments": 0,
                    "labels": [],
                }
            ],
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GitHubReadClient(
            read_token="mock_token",
            max_retries=3,
            http_client=http_client,
        )

        issues = await client.list_issues(repo="owner/repo")
        assert len(issues) == 1
        assert issues[0].title == "Recovered Issue"
        assert call_count == 3


@pytest.mark.asyncio
async def test_timeout_handling_and_exhaustion():
    """Verify that network timeouts trigger retries and raise timeout exceptions."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        raise httpx.ReadTimeout("Read operation timed out", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GitHubReadClient(
            read_token="mock_token",
            max_retries=2,
            http_client=http_client,
        )

        with pytest.raises(httpx.ReadTimeout):
            await client.list_issues(repo="owner/repo")

        assert call_count == 3


@pytest.mark.asyncio
async def test_pagination_drift_and_max_pages_cap():
    """Verify that Link header pagination is followed and capped by max_pages."""
    def handler(request: httpx.Request) -> httpx.Response:
        page_val = request.url.params.get("page")
        if page_val == "2":
            return httpx.Response(
                status_code=200,
                headers={"Link": '<https://api.github.com/repos/owner/repo/issues?page=3>; rel="next"'},
                json=[{"number": 2, "title": "Page 2 Issue", "created_at": "2026-11-17T12:00:00Z", "labels": []}],
            )
        elif page_val == "3":
            return httpx.Response(
                status_code=200,
                headers={"Link": '<https://api.github.com/repos/owner/repo/issues?page=4>; rel="next"'},
                json=[{"number": 3, "title": "Page 3 Issue", "created_at": "2026-11-17T12:00:00Z", "labels": []}],
            )
        else:
            return httpx.Response(
                status_code=200,
                headers={"Link": '<https://api.github.com/repos/owner/repo/issues?page=2>; rel="next"'},
                json=[{"number": 1, "title": "Page 1 Issue", "created_at": "2026-11-17T12:00:00Z", "labels": []}],
            )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        # Cap at max_pages=2
        client = GitHubReadClient(
            read_token="mock_token",
            max_pages=2,
            http_client=http_client,
        )

        issues = await client.list_issues(repo="owner/repo", max_pages=2)
        # Should collect Page 1 and Page 2 only (2 issues)
        assert len(issues) == 2
        assert [i.number for i in issues] == [1, 2]


@pytest.mark.asyncio
async def test_cyclic_pagination_link_protection():
    """Verify that cyclic Link headers do not cause an infinite loop."""
    def handler(request: httpx.Request) -> httpx.Response:
        # Returns a Link header pointing back to itself
        return httpx.Response(
            status_code=200,
            headers={"Link": '<https://api.github.com/repos/owner/repo/issues>; rel="next"'},
            json=[{"number": 10, "title": "Cyclic Issue", "created_at": "2026-11-17T12:00:00Z", "labels": []}],
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GitHubReadClient(
            read_token="mock_token",
            max_pages=10,
            http_client=http_client,
        )

        issues = await client.list_issues(repo="owner/repo")
        # Should detect cycle after the first repetition and terminate cleanly
        assert len(issues) == 1


@pytest.mark.asyncio
async def test_write_client_idempotency_protection():
    """Verify that duplicate comment writes with the same idempotency key are skipped."""
    posted_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posted_count
        posted_count += 1
        return httpx.Response(status_code=201, json={"id": 999, "body": "Comment text"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GitHubWriteClient(
            write_token="mock_write_token",
            http_client=http_client,
        )

        idempotency_key = "test_key_123"

        # First comment should succeed
        c1 = await client.add_comment(
            repo="owner/repo",
            issue_number=1,
            body="First attempt",
            idempotency_key=idempotency_key,
        )
        assert c1 == 999
        assert posted_count == 1

        # Second attempt with same idempotency key should be blocked by idempotency guard
        c2 = await client.add_comment(
            repo="owner/repo",
            issue_number=1,
            body="First attempt",
            idempotency_key=idempotency_key,
        )
        assert c2 is None
        assert posted_count == 1  # Network request not sent again
