import pytest

from scripts.release import check_version, release_key


@pytest.mark.parametrize(
    ("version", "current"),
    [("0.1.0", "0.1.0.dev0"), ("0.1.1", "0.1.0"), ("1.0.0", "0.9.9"), ("0.10.0", "0.9.0")],
)
def test_a_release_must_come_after_the_current_version(version: str, current: str) -> None:
    check_version(version, current)


@pytest.mark.parametrize(
    ("version", "current"),
    [("0.1.0", "0.1.0"), ("0.0.9", "0.1.0"), ("1.0", "0.1.0"), ("1.0.0rc1", "0.1.0")],
)
def test_other_versions_are_refused(version: str, current: str) -> None:
    with pytest.raises(SystemExit):
        check_version(version, current)


def test_a_development_version_comes_before_its_release() -> None:
    assert release_key("0.2.0.dev0") < release_key("0.2.0") < release_key("0.2.1.dev0")
