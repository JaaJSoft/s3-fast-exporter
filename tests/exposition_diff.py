from collections.abc import Set
from dataclasses import dataclass

from prometheus_client.parser import text_string_to_metric_families

type Labels = tuple[tuple[str, str], ...]

VARYING_VALUES = frozenset({"s3_list_duration_seconds"})


@dataclass(frozen=True, slots=True)
class Family:
    type: str
    help: str
    samples: dict[Labels, float]


def parse_exposition(text: str) -> dict[str, Family]:
    return {
        metric.name: Family(
            metric.type,
            metric.documentation,
            {
                tuple(sorted(sample.labels.items())): float(sample.value)
                for sample in metric.samples
            },
        )
        for metric in text_string_to_metric_families(text)
    }


def differences(expected: str, actual: str, *, varying: Set[str] = VARYING_VALUES) -> list[str]:
    want, got = parse_exposition(expected), parse_exposition(actual)
    problems = [f"missing family {name}" for name in sorted(want.keys() - got.keys())]
    problems += [f"unexpected family {name}" for name in sorted(got.keys() - want.keys())]
    for name in sorted(want.keys() & got.keys()):
        problems += _family_differences(name, want[name], got[name], name in varying)
    return problems


def _family_differences(name: str, want: Family, got: Family, varying: bool) -> list[str]:
    problems = []
    if want.type != got.type:
        problems.append(f"{name}: type {got.type!r}, expected {want.type!r}")
    if want.help != got.help:
        problems.append(f"{name}: help {got.help!r}, expected {want.help!r}")
    problems.extend(
        f"{name}{dict(labels)}: missing sample"
        for labels in sorted(want.samples.keys() - got.samples.keys())
    )
    problems.extend(
        f"{name}{dict(labels)}: unexpected sample"
        for labels in sorted(got.samples.keys() - want.samples.keys())
    )
    if not varying:
        problems.extend(
            f"{name}{dict(labels)}: {got.samples[labels]!r}, expected {want.samples[labels]!r}"
            for labels in sorted(want.samples.keys() & got.samples.keys())
            if want.samples[labels] != got.samples[labels]
        )
    return problems
