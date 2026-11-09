"""Reading YAML and JSON with the lines of every value (loader.py) and JSON pointers (pointer.py)."""

import pytest

from speclens import pointer
from speclens.loader import load


def load_text(tmp_path, text, name="spec.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return load(path, name)


def test_lines_start_at_the_key_and_end_at_the_last_value(tmp_path):
    doc = load_text(tmp_path, "a: 1\nb:\n  c: 2\n  d:\n    - x\n    - y\n\ne: 3\n")
    assert doc.lines["/a"] == (1, 1)
    assert doc.lines["/b"] == (2, 6)  # not the blank line after it
    assert doc.lines["/b/d/1"] == (6, 6)
    assert doc.lines["/e"] == (8, 8)
    assert doc.source("/b") == "b:\n  c: 2\n  d:\n    - x\n    - y"


def test_block_scalar_ends_on_its_last_line(tmp_path):
    doc = load_text(tmp_path, "a: |\n  one\n  two\nb: 1\n")
    assert doc.data["a"] == "one\ntwo\n"
    assert doc.lines["/a"] == (1, 3)


def test_json_is_read_with_lines(tmp_path):
    doc = load_text(tmp_path, '{\n  "a": {\n    "b": [1, 2]\n  },\n  "c": true\n}\n', "spec.json")
    assert doc.data == {"a": {"b": [1, 2]}, "c": True}
    assert doc.lines["/a"] == (2, 4)
    assert doc.lines["/c"] == (5, 5)


def test_keys_stay_as_written(tmp_path):
    # Response codes and words YAML 1.1 reads as booleans must stay strings.
    doc = load_text(tmp_path, "200: ok\non: x\n2XX: y\n")
    assert list(doc.data) == ["200", "on", "2XX"]


def test_values_keep_their_types_but_dates_stay_text(tmp_path):
    doc = load_text(tmp_path, "i: 1\nf: 1.5\nb: true\nn: null\nd: 2021-01-01\n")
    assert doc.data == {"i": 1, "f": 1.5, "b": True, "n": None, "d": "2021-01-01"}


def test_duplicate_key_is_reported(tmp_path):
    doc = load_text(tmp_path, "a: 1\na: 2\n")
    assert doc.data == {"a": 2}
    assert [(i.code, i.line) for i in doc.issues] == [("duplicate-key", 2)]


def test_parse_error_has_a_line(tmp_path):
    doc = load_text(tmp_path, "a: 1\nb: [unclosed\nc: 2\n")
    assert doc.data is None
    assert doc.issues[0].code == "parse-error" and doc.issues[0].line is not None


def test_alias_is_expanded(tmp_path):
    doc = load_text(tmp_path, "a: &x {k: 1}\nb: *x\n")
    assert doc.data["b"] == {"k": 1}


def test_pointer_escaping():
    assert pointer.join("", "paths", "/pets/{id}", "get") == "/paths/~1pets~1{id}/get"
    assert pointer.split("/paths/~1pets~1{id}/a~0b") == ["paths", "/pets/{id}", "a~b"]
    assert pointer.from_fragment("/components/schemas/My%20Pet") == "/components/schemas/My Pet"


def test_pointer_lookup():
    data = {"a": [{"b": 1}], "x/y": 2}
    assert pointer.lookup(data, "/a/0/b") == 1
    assert pointer.lookup(data, "/x~1y") == 2
    assert pointer.lookup(data, "") is data
    with pytest.raises(KeyError):
        pointer.lookup(data, "/a/5")


def test_plain_values_follow_yaml_12(tmp_path):
    # OpenAPI requires YAML 1.2; under YAML 1.1 these would be booleans, octal, base 60 and a "value" tag.
    doc = load_text(tmp_path, "enum: [yes, no, on, off, NO, y]\nzip: 010\ntime: 1:20\nop: =\n"
                              "hex: 0x1F\nok: true\nnone: ~\nbig: 1e3\nunder: 1_000\noct: 0o17\n")
    assert doc.data == {"enum": ["yes", "no", "on", "off", "NO", "y"], "zip": 10, "time": "1:20", "op": "=",
                        "hex": 31, "ok": True, "none": None, "big": 1000.0, "under": "1_000", "oct": 15}
    assert doc.issues == []


def test_a_tag_nobody_knows_keeps_its_text(tmp_path):
    doc = load_text(tmp_path, "a: !custom thing\nb: !!str 5\nc: '5'\nd: !!int '7'\n")
    assert doc.data == {"a": "thing", "b": "5", "c": "5", "d": 7}


def test_tabs_on_blank_lines_in_multi_line_text(tmp_path):
    doc = load_text(tmp_path, "a:\n  description: |-\n    \t\n    Text after a blank line.\nb: 1\n")
    assert doc.data == {"a": {"description": "\nText after a blank line."}, "b": 1}
    assert [(i.code, i.severity) for i in doc.issues] == [("tab-in-blank-line", "warning")]
    assert doc.lines["/b"] == (5, 5)  # same lines as the file
    assert "\t" in doc.text  # the source shown is the file as written


def test_control_characters_are_read_as_replacement(tmp_path):
    doc = load_text(tmp_path, "a:\n  description: caf\u0083 menu\nb: 1\n")
    assert doc.data == {"a": {"description": "caf\ufffd menu"}, "b": 1}
    assert [i.code for i in doc.issues] == ["control-character"]
    assert doc.lines["/b"] == (3, 3)


def test_a_harmless_tab_is_left_alone_when_another_character_is_repaired(tmp_path):
    doc = load_text(tmp_path, "a: 1\n  \t\nb: caf\u0083\n")  # YAML allows a tab on a blank line here
    assert [i.code for i in doc.issues] == ["control-character"]
    assert doc.data == {"a": 1, "b": "caf\ufffd"}


def test_a_file_nested_too_deep_is_refused(tmp_path):
    doc = load_text(tmp_path, "a: " + "{b: " * 300 + "1" + "}" * 300 + "\n")
    assert doc.data is None and [i.code for i in doc.issues] == ["too-large"]
    assert load_text(tmp_path, "a: " + "{b: " * 50 + "1" + "}" * 50 + "\n").data is not None


def test_aliases_that_multiply_a_file_are_refused(tmp_path):
    lines = ["a0: &a0 [x, y, z]"] + [f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}, *a{i - 1}]" for i in range(1, 30)]
    doc = load_text(tmp_path, "\n".join(lines) + "\n")  # 3^30 values in 30 lines
    assert doc.data is None and [i.code for i in doc.issues] == ["too-large"]
    assert "YAML aliases" in doc.issues[0].message
