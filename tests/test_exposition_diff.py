from exposition_diff import differences, parse_exposition

GO = """\
# HELP s3_objects The total number of objects for the bucket/prefix combination
# TYPE s3_objects gauge
s3_objects{bucket="b",prefix="p"} 3
# HELP s3_objects_size_sum_bytes The total size of all objects summed
# TYPE s3_objects_size_sum_bytes gauge
s3_objects_size_sum_bytes{bucket="b",prefix="p"} 1.5604596e+09
# HELP s3_list_duration_seconds The total duration of the list operation
# TYPE s3_list_duration_seconds gauge
s3_list_duration_seconds{bucket="b",delimiter="",prefix="p"} 0.25
"""


def test_float_formatting_and_label_order_do_not_matter() -> None:
    python = GO.replace("1.5604596e+09", "1560459600.0").replace(
        '{bucket="b",prefix="p"}', '{prefix="p",bucket="b"}'
    )
    assert differences(GO, python) == []


def test_duration_values_are_ignored_but_not_their_presence() -> None:
    assert differences(GO, GO.replace("} 0.25", "} 9.5")) == []
    without = "\n".join(line for line in GO.splitlines() if "duration" not in line) + "\n"
    assert differences(GO, without) == ["missing family s3_list_duration_seconds"]


def test_value_help_type_and_label_changes_are_reported() -> None:
    changed = (
        GO.replace("prefix combination", "prefix")
        .replace("s3_objects gauge", "s3_objects counter")
        .replace('s3_objects{bucket="b",prefix="p"} 3', 's3_objects{bucket="b",prefix="q"} 3')
        .replace("1.5604596e+09", "0")
    )
    assert differences(GO, changed) == [
        "s3_objects: type 'counter', expected 'gauge'",
        "s3_objects: help 'The total number of objects for the bucket/prefix', "
        "expected 'The total number of objects for the bucket/prefix combination'",
        "s3_objects{'bucket': 'b', 'prefix': 'p'}: missing sample",
        "s3_objects{'bucket': 'b', 'prefix': 'q'}: unexpected sample",
        "s3_objects_size_sum_bytes{'bucket': 'b', 'prefix': 'p'}: 0.0, expected 1560459600.0",
    ]


def test_parse_keeps_type_help_and_samples() -> None:
    family = parse_exposition(GO)["s3_objects"]
    assert family.type == "gauge"
    assert family.samples == {(("bucket", "b"), ("prefix", "p")): 3.0}
