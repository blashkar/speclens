"""Read one YAML or JSON file into plain Python values, keeping the lines of every value.

JSON is read through the YAML parser, since JSON is valid YAML. Plain values are read as YAML 1.2 says, which
OpenAPI requires: `yes`, `no`, `on` and `off` are text, not booleans, and `010` is ten. (PyYAML follows YAML 1.1,
so its own reading of those is not used.) Each value's lines are recorded by its JSON
pointer, so code that walks the plain values can always find where something was written.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import pointer
from .issues import ERROR, WARNING, Issue

_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_constructor = yaml.constructor.SafeConstructor()
# Tags the YAML 1.1 resolver gives plain values. A plain value with no tag written is read again with the YAML 1.2
# core schema, so `1e3` is a number and `yes` is text.
_IMPLICIT = {f"tag:yaml.org,2002:{t}" for t in ("null", "bool", "int", "float", "timestamp", "value", "merge", "str")}
_INT = re.compile(r"[-+]?[0-9]+")
_FLOAT = re.compile(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?")


def _core(text: str):
    """A plain scalar under the YAML 1.2 core schema."""
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if _INT.fullmatch(text):
        return int(text)
    if re.fullmatch(r"0o[0-7]+", text):
        return int(text[2:], 8)
    if re.fullmatch(r"0x[0-9a-fA-F]+", text):
        return int(text[2:], 16)
    if _FLOAT.fullmatch(text):
        return float(text)
    if re.fullmatch(r"[-+]?\.(inf|Inf|INF)", text):
        return float(text.replace(".", "", 1))
    if text in (".nan", ".NaN", ".NAN"):
        return float("nan")
    return text


def _scalar(node, text: str):
    # Plain (style None from the Python parser, "" from libyaml), and no tag written before it.
    tagged = text[node.start_mark.index:node.start_mark.index + 1] == "!"
    if not node.style and not tagged and node.tag in _IMPLICIT:
        return _core(node.value)
    if node.tag == "tag:yaml.org,2002:str":
        return node.value
    try:
        return _constructor.construct_object(node)
    except yaml.YAMLError:
        return node.value  # a tag nobody here knows: keep the text


@dataclass
class Document:
    path: Path  # absolute and resolved, so one file is never loaded twice
    name: str  # as shown to people: relative to the spec's root folder
    text: str
    data: object = None
    lines: dict[str, tuple[int, int]] = field(default_factory=dict)  # pointer -> (first line, last line), 1-based
    parsed: str = field(default="", repr=False)  # the text the parser read; differs from `text` only if repaired
    issues: list[Issue] = field(default_factory=list)

    def line_of(self, ptr: str) -> int | None:
        span = self.lines.get(ptr)
        return span[0] if span else None

    def source(self, ptr: str) -> str:
        """The original lines behind the value at `ptr`."""
        start, end = self.lines[ptr]
        return "\n".join(self.text.splitlines()[start - 1:end])


def load(path: Path, name: str) -> Document:
    doc = Document(path=path, name=name, text="")
    try:
        doc.text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        doc.issues.append(Issue(ERROR, "unreadable-file", str(e), name))
        return doc
    doc.parsed = doc.text
    try:
        node = yaml.compose(doc.text, Loader=_Loader)
    except yaml.YAMLError as e:
        node, error = None, e
        lenient, repaired = doc.text, []
        if _CONTROL.search(lenient):
            lenient = _CONTROL.sub("\ufffd", lenient)  # one character for one: positions and lines stay
            repaired.append(("control-character", "control characters YAML does not allow (often mis-encoded "
                                                  "text) were read as \ufffd"))
            node, error = _compose(lenient)
        # Tabs are repaired only when they are what stops the parse, so a harmless tab is not reported.
        if node is None and "tab character" in str(error):
            tabs = _without_blank_line_tabs(lenient)
            if tabs is not None:
                lenient = tabs
                repaired.append(("tab-in-blank-line", "tab characters on blank lines inside multi-line text "
                                                      "were ignored (YAML does not allow them there)"))
                node, error = _compose(lenient)
        if node is not None:
            doc.parsed = lenient
            mark = getattr(e, "problem_mark", None)
            for code, message in repaired:
                doc.issues.append(Issue(WARNING, code, message, name, mark.line + 1 if mark else None))
        if node is None:
            mark = getattr(e, "problem_mark", None)
            doc.issues.append(Issue(ERROR, "parse-error", str(e).replace("\n", " "), name,
                                    mark.line + 1 if mark else None))
            return doc
    if node is not None:
        refused = _too_big(node)
        if refused:
            doc.issues.append(Issue(ERROR, "too-large", refused + "; the file is not read", name))
            return doc
        doc.data, _ = _convert(node, "", doc, set())
    return doc


MAX_DEPTH = 200  # values inside values; real specs stay far below this
MAX_EXPANSION = 100_000  # values once YAML aliases are expanded, when aliases multiply the file


def _too_big(root) -> str | None:
    """Why a parsed file is refused, if it is: nested too deep to walk, or YAML aliases that repeat parts of it
    so often that the expanded file is huge (a few lines can stand for billions of values)."""
    size: dict[int, int] = {}  # node -> values in it once aliases are expanded
    depth: dict[int, int] = {}
    stack = [(root, False)]
    while stack:
        node, done = stack.pop()
        if id(node) in size:
            continue
        if isinstance(node, yaml.ScalarNode):
            size[id(node)], depth[id(node)] = 1, 1
            continue
        children = node.value if isinstance(node, yaml.SequenceNode) else [v for _, v in node.value]
        if not done:
            if id(node) in depth:  # open: an alias inside itself, reported when the file is read
                continue
            depth[id(node)] = 0
            stack.append((node, True))
            stack.extend((c, False) for c in children if id(c) not in size)
            continue
        size[id(node)] = 1 + sum(size.get(id(c), 0) for c in children)
        depth[id(node)] = 1 + max((depth.get(id(c), 0) for c in children), default=0)
        if depth[id(node)] > MAX_DEPTH:
            return f"values are nested more than {MAX_DEPTH} deep"
        if size[id(node)] > MAX_EXPANSION and size[id(node)] > 10 * len(size):
            return f"YAML aliases expand it to more than {MAX_EXPANSION:,} values"
    return None


def _compose(text: str):
    try:
        return yaml.compose(text, Loader=_Loader), None
    except yaml.YAMLError as e:
        return None, e


# What YAML allows is tab, newline, carriage return, printable ASCII, NEL and most of Unicode from U+00A0.
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f\ufffe\uffff]")


def _without_blank_line_tabs(text: str) -> str | None:
    """The text with tabs removed from lines that hold nothing but spaces and tabs; same lines, same numbers."""
    lines = text.split("\n")
    changed = [re.sub(r"\t", "", line) if line.strip(" \t") == "" and "\t" in line else line for line in lines]
    return "\n".join(changed) if changed != lines else None


def _end_line(mark) -> int:
    # A mark at the start of a line belongs to the line before it (block scalars end that way).
    return mark.line if mark.column == 0 and mark.line > 0 else mark.line + 1


def _convert(node, ptr: str, doc: Document, open_nodes: set, start: int | None = None):
    """Returns (value, last line), recording the lines of the value and everything inside it.

    `start` is the line of the value's key, when it has one, so a cited block begins with its name.
    """
    start = start or node.start_mark.line + 1
    if isinstance(node, yaml.ScalarNode):
        value = _scalar(node, doc.parsed)
        end = _end_line(node.end_mark)
    else:
        if id(node) in open_nodes:
            doc.issues.append(Issue(ERROR, "recursive-alias", "a YAML alias refers to itself", doc.name, start))
            return None, start
        open_nodes.add(id(node))
        end = start
        if isinstance(node, yaml.SequenceNode):
            value = []
            for i, item in enumerate(node.value):
                v, e = _convert(item, pointer.join(ptr, i), doc, open_nodes)
                value.append(v)
                end = max(end, e)
        else:
            value = {}
            for key_node, value_node in node.value:
                key = key_node.value if isinstance(key_node, yaml.ScalarNode) else str(key_node.value)
                if key in value:
                    doc.issues.append(Issue(WARNING, "duplicate-key", f"key {key!r} appears more than once; "
                                            "the last one is used", doc.name, key_node.start_mark.line + 1))
                v, e = _convert(value_node, pointer.join(ptr, key), doc, open_nodes, key_node.start_mark.line + 1)
                value[key] = v
                end = max(end, e)
        if node.flow_style:
            end = _end_line(node.end_mark)
        open_nodes.discard(id(node))
    doc.lines[ptr] = (start, end)
    return value, end
