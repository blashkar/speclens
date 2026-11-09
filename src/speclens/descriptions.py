"""Descriptions that state a limit the schema contradicts: "must not exceed 100 characters" on a field whose
`maxLength` is 120, "a number from 1 to 12" on an integer with no such bounds. A reader, or a model, may believe
the prose. Only limits: "Required" in prose is most often a condition ("required for mobile integrations") or a
vendor's convention, not a contradiction."""

import re

from . import graph as g
from .issues import WARNING, Issue
from .query import Ambiguous, NotFound
from .verify import verify

CODE = "description-contradicts-schema"
_NUMBER = re.compile(r"\d")
_LIMIT = re.compile(r": (?:maxLength|minLength|maximum|minimum|exclusiveMaximum|exclusiveMinimum|maxItems|minItems|"
                    r"maxProperties|minProperties|multipleOf|none)\b")


def description_issues(spec) -> list[Issue]:
    """A warning for each description of a schema, a field or a parameter that states a limit against what the
    schema says. Checked with `verify`, the description standing as an answer about the thing it describes, the
    thing's own result as the evidence. A description with no digit states no limit and is not read."""
    out: list[Issue] = []
    for node in spec.graph.of_kind(g.SCHEMA):
        if not node.data.get("named"):
            continue
        try:
            r = spec.get_schema(node.id)
        except (NotFound, Ambiguous):  # a schema the queries cannot show is reported elsewhere
            continue
        if _NUMBER.search(r.description or ""):  # "The name must not exceed 100 characters": its fields, named in plain words
            out += _checked(_with_names(r.description, [f.name for f in r.fields]), r, r.name, r.where)
        for f in r.fields:
            if _NUMBER.search(f.description or "") and not f.inherited_from:  # once, where it is written
                out += _checked(f"`{f.name}`: {f.description}", r, f"{r.name}.{f.name}", f.where)
    for node in spec.graph.of_kind(g.OPERATION):
        try:
            r = spec.get_operation(node.id)
        except (NotFound, Ambiguous):
            continue
        for p in r.parameters:
            if _NUMBER.search(p.description or ""):
                out += _checked(f"`{p.name}`: {p.description}", r, f"{r.method} {r.path} {p.location} {p.name}",
                                p.where)
    return list(dict.fromkeys(out))


def _with_names(text: str, names: list[str]) -> str:
    """The text with these names in backticks where they stand as words outside code: "the name" -> "the `name`"."""
    parts = re.split(r"(`[^`]*`)", text)
    for name in sorted(set(names), key=len, reverse=True):
        pattern = re.compile(rf"(?<![\w`.]){re.escape(name)}(?![\w`])")
        parts = [p if p.startswith("`") else pattern.sub(f"`{name}`", p) for p in parts]
    return "".join(parts)


def _checked(text: str, result, what: str, where: str) -> list[Issue]:
    file, line = _place(where)
    out = []
    for f in verify(text, [result]).findings:
        if f.kind == "contradicted-claim" and _LIMIT.search(f.detail):
            detail = re.sub(r" in \S+$", "", f.detail).replace("the evidence shows", "the schema says")
            said = re.sub(r"^`[^`]+`: ", "", f.text.strip())  # the description's words, not the name put before
            out.append(Issue(WARNING, CODE, f"{what}: the description says {said!r}; {detail}", file, line))
    return out


def _place(where: str) -> tuple[str | None, int | None]:
    m = re.fullmatch(r"(.+):(\d+)(?:-\d+)?", where or "")
    return (m.group(1), int(m.group(2))) if m else (where or None, None)
