from patchpilot.ratelimit import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_limit_per_client() -> None:
    limiter = RateLimiter(per_client=2, total=10, clock=Clock())

    assert [limiter.allow("a") for _ in range(3)] == [True, True, False]
    assert limiter.allow("b")


def test_limit_for_everyone_together() -> None:
    limiter = RateLimiter(per_client=2, total=3, clock=Clock())

    assert [limiter.allow(name) for name in "abcd"] == [True, True, True, False]


def test_requests_are_forgotten_after_a_minute() -> None:
    clock = Clock()
    limiter = RateLimiter(per_client=1, total=10, clock=clock)
    assert limiter.allow("a")

    clock.now += 59.9
    assert not limiter.allow("a")

    clock.now += 0.1
    assert limiter.allow("a")


def test_refused_requests_do_not_extend_the_wait() -> None:
    clock = Clock()
    limiter = RateLimiter(per_client=1, total=10, clock=clock)
    assert limiter.allow("a")
    for _ in range(100):
        clock.now += 0.5
        assert not limiter.allow("a")

    clock.now += 10

    assert limiter.allow("a")
