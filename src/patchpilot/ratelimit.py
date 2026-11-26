"""Limit how often a shared web server accepts requests."""

import time
from collections import deque
from collections.abc import Callable

_WINDOW_SECONDS = 60.0


class RateLimiter:
    """Accept at most `per_client` requests per client and `total` overall in any minute.

    Counts live in memory, so they reset on restart and cover one server process only. Use
    from a single thread: the web app calls it from its event loop.
    """

    def __init__(
        self, per_client: int, total: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._per_client = per_client
        self._total = total
        self._clock = clock
        # (time, client) for each accepted request, oldest first. Never longer than `total`.
        self._accepted: deque[tuple[float, str]] = deque()

    def allow(self, client: str) -> bool:
        now = self._clock()
        while self._accepted and now - self._accepted[0][0] >= _WINDOW_SECONDS:
            self._accepted.popleft()
        from_client = sum(1 for _, other in self._accepted if other == client)
        if len(self._accepted) >= self._total or from_client >= self._per_client:
            return False
        self._accepted.append((now, client))
        return True
