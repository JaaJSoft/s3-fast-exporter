"""What makes a listing fail without a bug: S3, the network, or a worker process."""

import aiohttp
from botocore.exceptions import BotoCoreError, ClientError

from exporter.s3.lister import S3Error
from exporter.s3.scanner import ProtocolError


class WorkerError(Exception):
    """A worker process could not list its ranges, or stopped while listing them."""


LISTING_ERRORS = (
    S3Error,
    ProtocolError,
    WorkerError,
    aiohttp.ClientError,
    TimeoutError,
    BotoCoreError,
    ClientError,
)
