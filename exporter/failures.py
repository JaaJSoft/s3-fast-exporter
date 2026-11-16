"""What makes a listing fail without a bug: S3, the network, or a worker process."""

import aiohttp
from botocore.exceptions import BotoCoreError, ClientError

from exporter.s3.lister import S3Error
from exporter.s3.scanner import ProtocolError


class WorkerError(Exception):
    """A worker process could not list a range, or stopped while listing it."""


class WorkerLostError(WorkerError):
    """The worker process stopped while listing a range: another one may list it."""


LISTING_ERRORS = (
    S3Error,
    ProtocolError,
    WorkerError,
    aiohttp.ClientError,
    TimeoutError,
    BotoCoreError,
    ClientError,
)
