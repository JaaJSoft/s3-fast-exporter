"""Set the project version, commit it on master and tag the release.

Run from the repository root: uv run python -m scripts.release X.Y.Z
Pushing the tag makes CI publish the X.Y.Z, X.Y and latest images.
"""

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = re.compile(r"\d+\.\d+\.\d+")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: uv run python -m scripts.release X.Y.Z")
    version = sys.argv[1].removeprefix("v")
    tag = f"v{version}"
    check_version(version, project_version())

    if git("branch", "--show-current") != "master":
        sys.exit("releases are tagged on master")
    if git("status", "--porcelain"):
        sys.exit("the working tree is not clean")
    git("fetch", "--quiet", "--tags", "origin")
    if git("rev-parse", "HEAD") != git("rev-parse", "origin/master"):
        sys.exit("master differs from origin/master: pull or push first")
    if git("tag", "--list", tag):
        sys.exit(f"{tag} already exists")

    run("uv", "version", version)
    run("uv", "lock", "--check")
    git("add", "pyproject.toml", "uv.lock")
    git("commit", "--quiet", "--message", f"release: {tag}")
    git("tag", "--annotate", tag, "--message", f"Release {tag}")
    print(f"Tagged {tag}. Publish it with: git push --atomic origin master {tag}")


def check_version(version: str, current: str) -> None:
    if not RELEASE.fullmatch(version):
        sys.exit(f"{version!r} is not an X.Y.Z version")
    if release_key(version) <= release_key(current):
        sys.exit(f"{version} does not come after the current version {current}")


def release_key(version: str) -> tuple[int, int, int, int]:
    """Orders X.Y.Z versions; a suffixed one (0.2.0.dev0) comes before its release."""
    match = re.match(r"(\d+)\.(\d+)\.(\d+)(.*)", version)
    if match is None:
        sys.exit(f"cannot read the version {version!r}")
    major, minor, patch, suffix = match.groups()
    return int(major), int(minor), int(patch), 0 if suffix else 1


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as pyproject:
        version: str = tomllib.load(pyproject)["project"]["version"]
    return version


def git(*args: str) -> str:
    return run("git", *args)


def run(*command: str) -> str:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode:
        sys.exit(f"{' '.join(command)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


if __name__ == "__main__":
    main()
