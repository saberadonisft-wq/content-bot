import asyncio

from app.crawlers.runtime import AdaptiveRateLimiter, RatePolicy


def test_rate_limiter_spaces_requests_per_key_without_cross_key_blocking() -> None:
    now = [0.0]
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    limiter = AdaptiveRateLimiter(
        RatePolicy(requests_per_second=2),
        clock=lambda: now[0],
        sleep=sleep,
    )

    async def run() -> None:
        await limiter.acquire("bluesky:account-a")
        await limiter.acquire("bluesky:account-a")
        await limiter.acquire("bluesky:account-b")

    asyncio.run(run())
    assert sleeps == [0.5]


def test_retry_after_and_exponential_penalty_are_capped_and_recover() -> None:
    now = [0.0]
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    limiter = AdaptiveRateLimiter(
        RatePolicy(
            requests_per_second=100,
            base_backoff_seconds=2,
            max_backoff_seconds=5,
        ),
        clock=lambda: now[0],
        sleep=sleep,
    )

    async def run() -> list[float]:
        first = await limiter.penalize("reddit", retry_after_seconds=4)
        await limiter.acquire("reddit")
        second = await limiter.penalize("reddit")
        third = await limiter.penalize("reddit")
        await limiter.reward("reddit")
        fourth = await limiter.penalize("reddit")
        return [first, second, third, fourth]

    penalties = asyncio.run(run())
    assert penalties == [4, 4, 5, 5]
    assert sleeps == [4]
