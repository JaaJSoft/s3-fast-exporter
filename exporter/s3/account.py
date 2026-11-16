"""botocore calls outside the listing path. They block: run them in worker threads."""

from botocore.exceptions import ClientError
from botocore.session import Session

from exporter.s3.lister import S3Error
from exporter.s3.presigner import AWS_ENDPOINT, Endpoint, s3_client


class Account:
    def __init__(self, session: Session, *, region: str, endpoint: Endpoint = AWS_ENDPOINT) -> None:
        self._client = s3_client(session, region, endpoint)

    def bucket_names(self) -> list[str]:
        pages = self._client.get_paginator("list_buckets").paginate()
        return [bucket["Name"] for page in pages for bucket in page.get("Buckets", [])]

    def bucket_region(self, bucket: str) -> str | None:
        try:
            response = self._client.head_bucket(Bucket=bucket)
        except ClientError as error:
            response = error.response
            if response["ResponseMetadata"]["HTTPStatusCode"] == 404:
                raise S3Error(404, "NoSuchBucket", "The specified bucket does not exist") from None
        # S3 names the bucket's region even when it refuses the request (403, 301).
        region: str | None = response["ResponseMetadata"]["HTTPHeaders"].get("x-amz-bucket-region")
        return region
