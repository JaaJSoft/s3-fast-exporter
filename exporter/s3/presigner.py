"""botocore owns credentials, endpoints and signing; the listing itself never uses its HTTP stack.

A presigned URL carries the whole SigV4 authentication in its query string, so any HTTP
client can send it as long as it does not re-encode it.
"""

import asyncio

from botocore.config import Config
from botocore.credentials import RefreshableCredentials
from botocore.session import Session

URL_TTL_SECONDS = 900


class Presigner:
    """Build it in a worker thread: resolving credentials may query IMDS or STS."""

    def __init__(
        self,
        session: Session,
        *,
        region: str,
        endpoint_url: str | None = None,
        path_style: bool = False,
        use_ssl: bool = True,
    ) -> None:
        self._credentials = session.get_credentials()
        if self._credentials is None:
            raise RuntimeError("no AWS credentials found")
        self._client = session.create_client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            use_ssl=use_ssl,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if path_style else "virtual"},
            ),
        )
        self._refresh = asyncio.Lock()

    async def list_objects_v2(
        self,
        bucket: str,
        *,
        prefix: str = "",
        delimiter: str = "",
        start_after: str = "",
        continuation_token: str | None = None,
    ) -> str:
        await self._refresh_credentials()
        params = {"Bucket": bucket, "Prefix": prefix, "EncodingType": "url"}
        if delimiter:
            params["Delimiter"] = delimiter
        if start_after:
            params["StartAfter"] = start_after
        if continuation_token is not None:
            params["ContinuationToken"] = continuation_token
        url: str = self._client.generate_presigned_url(
            "list_objects_v2", Params=params, ExpiresIn=URL_TTL_SECONDS
        )
        return url

    async def _refresh_credentials(self) -> None:
        # botocore refreshes expiring credentials synchronously, over the network.
        credentials = self._credentials
        if isinstance(credentials, RefreshableCredentials) and credentials.refresh_needed():
            async with self._refresh:
                if credentials.refresh_needed():
                    await asyncio.to_thread(credentials.get_frozen_credentials)
