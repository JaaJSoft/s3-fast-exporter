from importlib.metadata import version

from exporter import __version__


def test_version_comes_from_distribution_metadata() -> None:
    assert __version__ == version("s3-fast-exporter")
