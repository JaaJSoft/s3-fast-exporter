import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from botocore.session import Session
from servers import serve_app

from exporter.app import create_app
from exporter.config import parse_settings
from fakes3.app import create_app as create_fakes3
from fakes3.golden import golden_buckets


@pytest.fixture(scope="module")
def self_signed_s3(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """fakes3 over HTTPS, with a certificate no CA signed, for a name that is not its host."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is not installed")
    directory = tmp_path_factory.mktemp("tls")
    certificate, key = directory / "certificate.pem", directory / "key.pem"
    subprocess.run(  # noqa: S603
        [openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-days", "1", "-subj", "/CN=s3.invalid",
         "-keyout", str(key), "-out", str(certificate)],
        check=True,
        capture_output=True,
    )  # fmt: skip
    tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    tls.load_cert_chain(Path(certificate), Path(key))
    with serve_app(create_fakes3(golden_buckets()), tls) as url:
        yield url


def exporter_flags(s3_url: str, *flags: str) -> list[str]:
    return [f"--s3.endpoint-url={s3_url}", "--s3.force-path-style", *flags]


@pytest.mark.parametrize("processes", [1, 2])
def test_probes_reach_s3_without_checking_its_certificate(
    aws_session: Session, monkeypatch: pytest.MonkeyPatch, self_signed_s3: str, processes: int
) -> None:
    # Worker processes resolve their own credentials.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDFAKES3")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fakes3-secret")
    flags = exporter_flags(
        self_signed_s3, "--s3.insecure-skip-verify", f"--s3.list-processes={processes}"
    )
    with (
        serve_app(create_app(parse_settings(flags, {}), aws_session)) as url,
        urllib.request.urlopen(f"{url}/probe?bucket=single", timeout=30) as answer,
    ):
        body = answer.read().decode()
    assert 's3_list_success{bucket="single",delimiter="",prefix=""} 1.0' in body
    assert 's3_objects{bucket="single",prefix=""} 1.0' in body


def test_discovery_reaches_s3_without_checking_its_certificate(
    aws_session: Session, self_signed_s3: str
) -> None:
    flags = exporter_flags(self_signed_s3, "--s3.insecure-skip-verify")
    with (
        serve_app(create_app(parse_settings(flags, {}), aws_session)) as url,
        urllib.request.urlopen(f"{url}/discovery", timeout=30) as answer,
    ):
        body = answer.read().decode()
    assert '"__param_bucket":"golden"' in body


def test_certificates_are_checked_by_default(aws_session: Session, self_signed_s3: str) -> None:
    settings = parse_settings(exporter_flags(self_signed_s3), {})
    with serve_app(create_app(settings, aws_session)) as url:
        with urllib.request.urlopen(f"{url}/probe?bucket=single", timeout=30) as answer:
            body = answer.read().decode()
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"{url}/discovery", timeout=30)
    assert 's3_list_success{bucket="single",delimiter="",prefix=""} 0.0' in body
    assert error.value.code == 500
