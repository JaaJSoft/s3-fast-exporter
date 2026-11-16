import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress


class Throttle:
    """Limits the requests in flight, as TCP limits the packets in flight.

    S3 answering 503 halves the limit; each round of answered requests raises it by one, back up
    to its ceiling. The 503 answers to requests sent before a halving come from the pace that
    caused it: they do not halve the limit again.
    """

    def __init__(self, ceiling: int) -> None:
        self._ceiling = float(ceiling)
        self._limit = float(ceiling)
        self._in_flight = 0
        self._waiting: deque[asyncio.Future[None]] = deque()
        self._halvings = 0

    @property
    def limit(self) -> int:
        return int(self._limit)

    @property
    def idle(self) -> bool:
        return self._in_flight == 0 and not self._waiting

    @asynccontextmanager
    async def request(self) -> AsyncIterator[int]:
        """Holds room for one request; yields the pace it is sent at, for slowed_down."""
        await self._acquire()
        try:
            yield self._halvings
        finally:
            self._release()

    def answered(self) -> None:
        self._limit = min(self._ceiling, self._limit + 1 / self._limit)
        self._wake()

    def slowed_down(self, pace: int) -> None:
        if pace == self._halvings:
            self._halvings += 1
            self._limit = max(1.0, self._limit / 2)

    async def _acquire(self) -> None:
        if not self._waiting and self._in_flight < self.limit:
            self._in_flight += 1
            return
        waiter = asyncio.get_running_loop().create_future()
        self._waiting.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.cancelled():
                with suppress(ValueError):
                    self._waiting.remove(waiter)
            else:
                # Cancelled just after being handed room: hand it on.
                self._release()
            raise

    def _release(self) -> None:
        self._in_flight -= 1
        self._wake()

    def _wake(self) -> None:
        while self._waiting and self._in_flight < self.limit:
            waiter = self._waiting.popleft()
            if not waiter.done():
                self._in_flight += 1
                waiter.set_result(None)
