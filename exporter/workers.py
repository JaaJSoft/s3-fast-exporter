"""Worker processes: each lists ranges with its own event loop, connections and core.

A process reads about 700 pages a second on one core. A worker process lists the ranges the
exporter's process hands it, as the exporter's process would, and sends back what it found. The
processes exchange pickled messages over pipes: they are one program, and trust each other.
"""

import asyncio
import itertools
import logging
import multiprocessing
import threading
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from multiprocessing.connection import Connection
from urllib.request import getproxies_environment

from botocore.session import get_session

from exporter.config import Settings
from exporter.failures import LISTING_ERRORS, WorkerError, WorkerLostError
from exporter.logs import configure_logging
from exporter.loop import new_event_loop
from exporter.ranges import LocalWorker, RangeJob
from exporter.s3.client import http_client
from exporter.s3.presigner import Endpoint, Presigner
from exporter.s3.proxies import Proxies
from exporter.s3.regions import Regions
from exporter.scan import Scan

log = logging.getLogger(__name__)

# spawn starts each worker from a fresh interpreter: a fork would copy the event loop and the
# threads of this process.
CONTEXT = multiprocessing.get_context("spawn")
STOP_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    range: RangeJob


@dataclass(frozen=True, slots=True)
class Cancel:
    id: int


@dataclass(frozen=True, slots=True)
class Listed:
    id: int
    scan: Scan


@dataclass(frozen=True, slots=True)
class Failed:
    id: int
    failure: str
    # A bug in the worker, rather than S3's answer or the network.
    bug: bool = False


class ProcessWorker:
    """A worker process, started again when it stops."""

    def __init__(self, settings: Settings, endpoint: Endpoint) -> None:
        self._settings = settings
        self._endpoint = endpoint
        self._ids = itertools.count()
        self._process = _Process(settings, endpoint, asyncio.get_running_loop())

    async def list(self, job: RangeJob) -> Scan:
        if not self._process.running:
            self._process = _Process(self._settings, self._endpoint, asyncio.get_running_loop())
        return await self._process.run(Job(next(self._ids), job))

    def stop(self) -> None:
        self._process.stop()

    def join(self) -> None:
        self._process.join()


async def stop(workers: Sequence[ProcessWorker]) -> None:
    """Stops the workers, killing those still running after a few seconds."""
    for worker in workers:
        worker.stop()
    await asyncio.gather(*(asyncio.to_thread(worker.join) for worker in workers))


class _Process:
    def __init__(
        self, settings: Settings, endpoint: Endpoint, loop: asyncio.AbstractEventLoop
    ) -> None:
        jobs, self._jobs = CONTEXT.Pipe(duplex=False)
        results, outcomes = CONTEXT.Pipe(duplex=False)
        self._process = CONTEXT.Process(
            target=work,
            args=(jobs, outcomes, settings, endpoint),
            name="s3-fast-exporter worker",
            daemon=True,
        )
        self._process.start()
        # Only the worker holds these ends now: reading its outcomes ends when it stops.
        jobs.close()
        outcomes.close()
        self._loop = loop
        self._pending: dict[int, asyncio.Future[Listed | Failed]] = {}
        self._stopping = False
        self.running = True
        threading.Thread(target=self._receive, args=(results,), daemon=True).start()

    async def run(self, job: Job) -> Scan:
        outcome: asyncio.Future[Listed | Failed] = self._loop.create_future()
        self._pending[job.id] = outcome
        try:
            try:
                self._jobs.send(job)
            except OSError:
                raise WorkerLostError("the worker process stopped") from None
            result = await outcome
        except asyncio.CancelledError:
            if self._pending.pop(job.id, None) is not None:
                with suppress(OSError):
                    self._jobs.send(Cancel(job.id))
            raise
        finally:
            self._pending.pop(job.id, None)
        if isinstance(result, Failed):
            raise (RuntimeError if result.bug else WorkerError)(result.failure)
        return result.scan

    def stop(self) -> None:
        self._stopping = True
        with suppress(OSError):
            self._jobs.send(None)
        self._jobs.close()

    def join(self) -> None:
        self._process.join(STOP_SECONDS)
        if self._process.is_alive():
            self._process.kill()
            self._process.join()

    def _receive(self, results: Connection) -> None:
        with suppress(EOFError, OSError):
            while True:
                message = results.recv()
                self._loop.call_soon_threadsafe(self._settle, message)
        results.close()
        self._process.join(STOP_SECONDS)
        # The event loop may be closed already, when the exporter stops.
        with suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._stopped, self._process.exitcode)

    def _settle(self, message: Listed | Failed) -> None:
        outcome = self._pending.get(message.id)
        if outcome is not None and not outcome.done():
            outcome.set_result(message)

    def _stopped(self, exit_code: int | None) -> None:
        self.running = False
        if not self._stopping:
            log.warning("worker process stopped", extra={"exit_code": exit_code})
        for outcome in self._pending.values():
            if not outcome.done():
                outcome.set_exception(WorkerLostError("the worker process stopped"))


def work(jobs: Connection, outcomes: Connection, settings: Settings, endpoint: Endpoint) -> None:
    """A worker process: lists the ranges it receives, until told to stop."""
    configure_logging(settings.log_level, settings.log_format, settings.log_stream)
    with asyncio.Runner(loop_factory=new_event_loop) as runner:
        runner.run(_work(jobs, outcomes, settings, endpoint))


async def _work(
    jobs: Connection, outcomes: Connection, settings: Settings, endpoint: Endpoint
) -> None:
    inbox: asyncio.Queue[Job | Cancel | None] = asyncio.Queue()
    forward = (jobs, asyncio.get_running_loop(), inbox)
    threading.Thread(target=_forward, args=forward, daemon=True).start()
    session = get_session()
    regions = Regions(lambda region: Presigner(session, region=region, endpoint=endpoint))
    running: dict[int, asyncio.Task[Scan]] = {}
    async with http_client(session, settings.list_concurrency) as http:
        local = LocalWorker(
            http,
            regions,
            concurrency=settings.list_concurrency,
            proxies=Proxies(getproxies_environment()),
        )
        while (message := await inbox.get()) is not None:
            if isinstance(message, Cancel):
                if (task := running.pop(message.id, None)) is not None:
                    task.cancel()
                continue
            task = asyncio.create_task(local.list(message.range))
            running[message.id] = task
            task.add_done_callback(partial(_report, outcomes, running, message.id))
        for task in running.values():
            task.cancel()


def _forward(
    jobs: Connection,
    loop: asyncio.AbstractEventLoop,
    inbox: asyncio.Queue[Job | Cancel | None],
) -> None:
    """Hands the messages of the exporter's process to the event loop; None stops the worker."""
    with suppress(EOFError, OSError):
        while (message := jobs.recv()) is not None:
            loop.call_soon_threadsafe(inbox.put_nowait, message)
    loop.call_soon_threadsafe(inbox.put_nowait, None)


def _report(
    outcomes: Connection, running: dict[int, asyncio.Task[Scan]], job: int, task: asyncio.Task[Scan]
) -> None:
    if running.pop(job, None) is None or task.cancelled():
        return
    error = task.exception()
    if error is None:
        message: Listed | Failed = Listed(job, task.result())
    elif isinstance(error, LISTING_ERRORS):
        message = Failed(job, str(error))
    else:
        log.error("listing failed in a worker process", exc_info=error)
        message = Failed(job, repr(error), bug=True)
    # The exporter's process may be gone: the worker stops when it reads the end of its jobs.
    with suppress(OSError):
        outcomes.send(message)
