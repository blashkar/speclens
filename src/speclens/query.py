"""The questions speclens answers. Each returns a small result with `to_text()` (compact, fixed wording, with
ids and file:line to cite) and `to_dict()`.

A name can be given as an id (`v1:schema/Pet`), as written (`Pet`), in any case (`pet`), or in plain words
(`pets`, `pet owner`). A name that matches nothing raises NotFound with the closest names; one that
matches several raises Ambiguous with all of them.
"""

import re
from dataclasses import asdict, dataclass, field
from itertools import combinations

from . import graph as g
from .graph import Graph, Node
from .text import PATH, first_line, full_text, key, trim_path, variants, words

METHODS = ("GET", "PUT", "POST", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE")


class NotFound(LookupError):
    def __init__(self, what: str, name: str, suggestions=(), labels=()):
        self.what, self.name, self.suggestions, self.labels = what, name, list(suggestions), list(labels)
        message = f"no {what} named {name!r}"
        if self.suggestions:
            message += f"; closest: {', '.join(self.suggestions)}"
        if self.labels:
            message += f"; labels: {', '.join(self.labels)}"
        super().__init__(message)


class Ambiguous(LookupError):
    def __init__(self, what: str, name: str, candidates):
        self.what, self.name, self.candidates = what, name, list(candidates)
        super().__init__(f"{what} {name!r} matches {len(self.candidates)}: {', '.join(self.candidates)}")


class Result:
    """A query's answer. to_dict() holds descriptions in full; to_text() cuts long ones to keep a prompt short,
    unless `full=True`."""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_text(self, full: bool = False) -> str:
        raise NotImplementedError

    def __str__(self):
        return self.to_text()


# How long a description is in to_text() unless `full`: a row's, and a schema's or operation's own.
ROW_LIMIT, MAIN_LIMIT = 100, 300


def _cut(text: str, limit: int, full: bool) -> str:
    return first_line(text, None if full else limit)


def _cite(node_id: str, where: str) -> str:
    return f"[{node_id} {where}]"


# ---- describing types ---------------------------------------------------------------------------

ENUM_SHOWN = 8  # values of an enum in the default text; full text and JSON have them all


def _enum_text(values: list, cap: int | None = ENUM_SHOWN) -> str:
    shown = values if cap is None else values[:cap]
    return "enum [" + ", ".join(repr(v) for v in shown) + (", …" if len(values) > len(shown) else "") + "]"


ROW_ENUM_FULL = 20  # a row's full text lists every value up to this many (most enums in public specs)
_NAMED_ENUM = re.compile(r"(\w+) \((?:[^()\[\]]*? )?enum \[")  # `Code (string enum [...` on a row


def _full_enum(text: str, values: list | None, full: bool, row: bool = False) -> str:
    """A type text with every enum value, when `full` and the default text cut them short. On a row, a long
    enum held in a named schema keeps the first values and says where the rest are, since the schema's own text
    lists them all: repeated on every row that holds it, a list of country codes crowds out the rest."""
    if not (full and values and len(values) > ENUM_SHOWN):
        return text
    named = _NAMED_ENUM.search(text) if row and len(values) > ROW_ENUM_FULL else None
    if named:
        more = f", … {len(values) - ENUM_SHOWN} more in {named.group(1)}]"
        return text.replace(_enum_text(values), _enum_text(values)[:-len(", …]")] + more)
    return text.replace(_enum_text(values), _enum_text(values, None))


def enum_values(graph: Graph, node: Node) -> list | None:
    """Every value a field, parameter or schema allows, when it is an enum (or const): through `$ref` and a lone
    allOf parent, as `describe` goes."""
    for _ in range(6):
        if node.kind in (g.FIELD, g.PARAMETER):
            kind = g.FIELD_TYPE if node.kind == g.FIELD else g.PARAM_TYPE
            edges = graph.outgoing(node.id, kind)
            if not edges:
                break
            node = graph.nodes[edges[0].target]
            continue
        if isinstance(node.data.get("enum"), list):
            return list(node.data["enum"])
        if "const" in node.data:
            return [node.data["const"]]
        parents = graph.outgoing(node.id, g.INHERITS)
        if len(parents) != 1 or graph.outgoing(node.id, g.HAS_FIELD) or node.data.get("type"):
            break
        node = graph.nodes[parents[0].target]
    d = node.data
    return list(d["enum"]) if isinstance(d.get("enum"), list) else [d["const"]] if "const" in d else None


def _scalar(data: dict) -> str:
    t = data.get("type")
    text = "|".join(map(str, t)) if isinstance(t, list) else (str(t) if t else "")
    if data.get("format"):
        text += f" ({data['format']})"
    if data.get("nullable"):
        text += "|null"
    if "const" in data:
        text = (text + " " if text else "") + f"const {data['const']!r}"
    if isinstance(data.get("enum"), list):
        text = (text + " " if text else "") + _enum_text(data["enum"])
    return text or "any"


def describe(graph: Graph, node: Node, depth: int = 0, name_refs: bool = False) -> str:
    """A type in a few words: `string (date)`, `Pet`, `array of Tag`, `one of Cat | Dog`, `object`.
    `name_refs`: a field or parameter whose type is a named schema says its name too: `Coordinates`,
    `ShipmentStatus (string enum [...])`."""
    if node.kind in (g.FIELD, g.PARAMETER):
        kind = g.FIELD_TYPE if node.kind == g.FIELD else g.PARAM_TYPE
        edges = graph.outgoing(node.id, kind)
        if edges:
            target = graph.nodes[edges[0].target]
            text = describe(graph, target, depth)
            if name_refs and target.data.get("named") and text != target.name:
                return target.name if text == "object" else f"{target.name} ({text})"
            return text
        return _scalar(node.data)
    if node.data.get("named") and depth > 0:
        return node.name
    if depth > 3:
        return node.name
    items = [e for e in graph.outgoing(node.id, g.ITEMS) if e.data.get("keyword") == "items"]
    if items:
        return "array of " + describe(graph, graph.nodes[items[0].target], depth + 1)
    options = graph.outgoing(node.id, g.OPTION)
    has_fields = bool(graph.outgoing(node.id, g.HAS_FIELD))
    if options and not has_fields:
        word = "one of" if options[0].data.get("keyword") == "oneOf" else "any of"
        return f"{word} " + " | ".join(describe(graph, graph.nodes[e.target], depth + 1) for e in options)
    parents = graph.outgoing(node.id, g.INHERITS)
    if len(parents) == 1 and not has_fields and not node.data.get("type"):
        return describe(graph, graph.nodes[parents[0].target], depth + 1)
    if node.data.get("named") and depth == 0:
        return _scalar(node.data) if not has_fields else (node.data.get("type") or "object")
    if has_fields or parents:
        return "object"
    return _scalar(node.data)


def _flags(data: dict) -> list[str]:
    return [f for f in ("deprecated", "readOnly", "writeOnly") if data.get(f)]


LIMIT_KEYS = ("minLength", "maxLength", "minimum", "exclusiveMinimum", "maximum", "exclusiveMaximum", "minItems",
              "maxItems", "multipleOf")


def limits(graph: Graph, node: Node) -> dict:
    """The limits on a field's or parameter's value, as written: on it, or on the schema it is (through `$ref`
    and `allOf`), not on its array items, and the `pattern` it must match. OpenAPI 3.0's boolean
    `exclusiveMaximum: true` makes `maximum` exclusive."""
    out: dict = {}
    todo, seen = [node], set()
    while todo and len(seen) < 8:
        n = todo.pop(0)
        if n.id in seen:
            continue
        seen.add(n.id)
        d = n.data
        for low, high in (("minimum", "exclusiveMinimum"), ("maximum", "exclusiveMaximum")):
            if d.get(high) is True and _is_number(d.get(low)) and high not in out and low not in out:
                out[high] = d[low]
        for k in LIMIT_KEYS:
            if k not in out and _is_number(d.get(k)) and not (k in ("minimum", "maximum") and
                                                               d.get("exclusive" + k[0].upper() + k[1:]) is True):
                out[k] = d[k]
        if "pattern" not in out and isinstance(d.get("pattern"), str):
            out["pattern"] = d["pattern"]
        for kind in (g.FIELD_TYPE, g.PARAM_TYPE, g.INHERITS):
            todo += [graph.nodes[e.target] for e in graph.outgoing(n.id, kind)]
    return {k: out[k] for k in (*LIMIT_KEYS, "pattern") if k in out}


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _limit_text(lims: dict) -> list[str]:
    return [f"{k} {v}" for k, v in lims.items()]


# ---- get_schema ---------------------------------------------------------------------------------

@dataclass
class FieldRow:
    name: str
    id: str
    type: str
    required: bool
    where: str
    description: str = ""
    inherited_from: str | None = None  # the schema the field is written in, when inherited
    via: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    limits: dict = field(default_factory=dict)  # maxLength, minimum, maxItems, ... as written
    enum: list | None = None  # every value, when it is an enum; the type text shows the first few

    def line(self, full: bool = False, owner: bool = True) -> str:
        """The row; `owner=False` leaves out "inherited from X" when a heading above the row says it."""
        parts = [_full_enum(self.type, self.enum, full, row=True)] + _limit_text(self.limits) + (["required"] if self.required else []) + self.flags
        if self.inherited_from and owner:
            parts.append(f"inherited from {self.inherited_from.split('/', 1)[1]}")
        text = f"{self.name}: {', '.join(parts)} {_cite(self.id, self.where)}"
        return text + (f" — {_cut(self.description, ROW_LIMIT, full)}" if self.description else "")


def _field_lines(fields: list[FieldRow], indent: str, full: bool) -> list[str]:
    """Own fields, then the inherited ones under one heading per schema they are written in, so a parent's
    name is said once, not on each of its rows. A single inherited row keeps the name on its own line."""
    lines = [indent + f.line(full) for f in fields if not f.inherited_from]
    groups: dict[str, list[FieldRow]] = {}
    for f in fields:
        if f.inherited_from:
            groups.setdefault(f.inherited_from, []).append(f)
    for owner, rows in groups.items():
        if len(rows) == 1:
            lines.append(indent + rows[0].line(full))
            continue
        lines.append(f"{indent}inherited from {owner.split('/', 1)[1]} ({len(rows)}):")
        lines += [f"{indent}  {f.line(full, owner=False)}" for f in rows]
    return lines


@dataclass
class OptionRow:
    id: str
    type: str
    keyword: str
    values: list[str] = field(default_factory=list)


@dataclass
class SchemaResult(Result):
    id: str
    name: str
    where: str
    type: str
    description: str
    fields: list[FieldRow]
    conditional_fields: list[str]  # ids of schemas holding fields that apply only under a condition
    options: list[OptionRow]
    discriminator: str | None
    used_by: list[str]
    enum: list | None = None  # every value, when the schema is an enum
    # A discriminator on this schema, oneOf or allOf alike: {"propertyName", "mapping": {value: schema id}}
    discriminator_mapping: dict | None = None
    extended_by: list[str] = field(default_factory=list)  # ids of schemas that take this one in with allOf
    expanded: list["SchemaResult"] = field(default_factory=list)  # with expand: the schemas its fields hold
    omitted: list[str] = field(default_factory=list)  # ids of schemas expand left out to stay within max_chars

    def to_text(self, full: bool = False) -> str:
        return _with_expanded(self._text(full), self.expanded, self.omitted, full)

    def _text(self, full: bool) -> str:
        lines = [f"schema {self.name} {_cite(self.id, self.where)}", f"type: {_full_enum(self.type, self.enum, full)}"]
        if self.description:
            lines.append(f"description: {_cut(self.description, MAIN_LIMIT, full)}")
        own = sum(1 for f in self.fields if not f.inherited_from)
        lines.append(f"fields ({len(self.fields)}: {own} own, {len(self.fields) - own} inherited):"
                     if self.fields else "fields: none")
        lines += _field_lines(self.fields, "  ", full)
        if self.options:
            head = f"options ({self.options[0].keyword}"
            head += f", discriminator {self.discriminator!r}):" if self.discriminator else "):"
            lines.append(head)
            for o in self.options:
                values = f" when {self.discriminator} = " + " or ".join(repr(v) for v in o.values) if o.values else ""
                lines.append(f"  {o.type} [{o.id}]{values}")
        if self.discriminator_mapping and not self.options:
            m = self.discriminator_mapping
            lines.append(f"discriminator {m['propertyName']!r}: " + ", ".join(
                f"{value!r} -> {target.split('/', 1)[1]}" for value, target in m["mapping"].items()))
        if self.extended_by:
            lines.append("extended by (allOf): " + ", ".join(x.split("/", 1)[1] for x in self.extended_by))
        if self.conditional_fields:
            lines.append("conditional (fields that apply only under a condition, not counted above): "
                         + ", ".join(self.conditional_fields))
        if self.used_by:
            shown = ", ".join(u.split("/", 1)[1] for u in self.used_by[:10])
            more = f" and {len(self.used_by) - 10} more" if len(self.used_by) > 10 else ""
            lines.append(f"used by {len(self.used_by)} operation{'s' * (len(self.used_by) != 1)}: {shown}{more}")
        else:
            lines.append("used by no operation")
        return "\n".join(lines)


def field_rows(graph: Graph, schema: Node) -> list[FieldRow]:
    rows = []
    for e in graph.outgoing(schema.id, g.HAS_FIELD):
        f = graph.nodes[e.target]
        owner = None
        if e.data["inherited"]:
            owners = [o.source for o in graph.incoming(f.id, g.HAS_FIELD) if not o.data["inherited"]]
            owner = owners[0] if owners else None
        rows.append(FieldRow(name=f.data["name"], id=f.id, type=describe(graph, f, name_refs=True),
                             required=e.data["required"], where=f.where(),
                             description=full_text(f.data.get("description")), inherited_from=owner,
                             via=list(e.data["via"]), flags=_flags(f.data), limits=limits(graph, f),
                             enum=enum_values(graph, f)))
    return rows


def _with_expanded(text: str, expanded: list, omitted: list[str], full: bool) -> str:
    if omitted:
        text += "\nreferenced, not shown (over max_chars): " + ", ".join(i.split("/", 1)[1] for i in omitted)
    if not expanded:
        return text
    return "\n\n".join([text + f"\nreferenced schemas ({len(expanded)}):"] + [e.to_text(full) for e in expanded])


def _fit(result, max_chars: int | None):
    """Leave out expanded schemas, the deepest first, until the text fits in `max_chars`; their ids stay listed
    on the result that held them. The result itself is always given whole."""
    if max_chars is None:
        return result
    while len(result.to_text()) > max_chars:
        deepest, stack = None, [(result, 0)]
        while stack:  # the last of the deepest
            r, depth = stack.pop()
            for child in r.expanded:
                if deepest is None or depth + 1 >= deepest[2]:
                    deepest = (r, child, depth + 1)
                stack.append((child, depth + 1))
        if deepest is None:
            break
        parent, child, _ = deepest
        parent.expanded.remove(child)
        parent.omitted.append(child.id)
    return result


def _named_targets(graph: Graph, node_id: str, depth: int = 0) -> list[str]:
    """The named schemas a schema is or holds through arrays, options and maps: `Pet`, `Pet[]`, `Cat | Dog`."""
    node = graph.nodes.get(node_id)
    if node is None or node.kind != g.SCHEMA:
        return []
    if node.data.get("named"):
        return [node_id]
    if depth > 3:
        return []
    out = []
    for kind in (g.ITEMS, g.OPTION, g.VALUES, g.INHERITS):
        for e in graph.outgoing(node_id, kind):
            out += _named_targets(graph, e.target, depth + 1)
    return list(dict.fromkeys(out))


def _expand(spec, targets: list[str], expand: int, skip: set[str]) -> list["SchemaResult"]:
    """get_schema of each target, with one level fewer of its own."""
    out = []
    for t in dict.fromkeys(targets):
        if t not in skip:
            skip.add(t)
            out.append(get_schema(spec, t, expand - 1, None, skip))
    return out


def get_schema(spec, name: str, expand: int = 0, max_chars: int | None = None,
               _seen: set[str] | None = None) -> SchemaResult:
    """`expand=1` also gives the named schemas its fields and options hold, each as a result of its own;
    `expand=2` theirs too. Each schema is given once. `max_chars` caps the text: expanded schemas are left out,
    the deepest first, and listed by name."""
    graph, index = spec.graph, spec.index
    node = _one(spec, g.SCHEMA, name, "schema")
    discriminator, options = None, []
    for e in graph.outgoing(node.id, g.OPTION):
        discriminator = discriminator or e.data.get("discriminator")
        options.append(OptionRow(id=e.target, type=describe(graph, graph.nodes[e.target], 1),
                                 keyword=e.data["keyword"], values=list(e.data.get("values", []))))
    conditional = [e.target for e in graph.outgoing(node.id, g.CONDITIONAL)]
    conditional += [o.id for o in options if graph.outgoing(o.id, g.HAS_FIELD)]
    used = list(dict.fromkeys(u["operation"] for u in index.used_by(node.id)))
    fields = field_rows(graph, node)
    expanded = []
    if expand > 0:
        seen = _seen if _seen is not None else {node.id}
        held = [t for f in fields for e in graph.outgoing(f.id, g.FIELD_TYPE) for t in _named_targets(graph, e.target)]
        held += [t for o in options for t in _named_targets(graph, o.id)]
        expanded = _expand(spec, held, expand, seen)
    return _fit(SchemaResult(id=node.id, name=node.name, where=node.where(), type=describe(graph, node),
                             description=full_text(node.data.get("description")), fields=fields,
                             conditional_fields=conditional, options=options, discriminator=discriminator,
                             used_by=used, expanded=expanded, enum=enum_values(graph, node),
                             discriminator_mapping=node.data.get("discriminator"),
                             extended_by=sorted({e.source for e in graph.incoming(node.id, g.INHERITS)
                                                 if e.data.get("via") == "allOf"})), max_chars)


# ---- get_operation ------------------------------------------------------------------------------

@dataclass
class ParameterRow:
    location: str  # path, query, header, cookie
    name: str
    id: str
    type: str
    required: bool
    where: str
    description: str = ""
    flags: list[str] = field(default_factory=list)
    limits: dict = field(default_factory=dict)
    enum: list | None = None  # every value, when it is an enum


@dataclass
class BodyRow:
    media_type: str | None
    schema: str  # node id
    type: str
    status: str | None = None  # for a response
    header: str | None = None  # a response header instead of a body
    required: bool = False  # for a request body
    fields: list[FieldRow] = field(default_factory=list)  # one level deep, for success responses


@dataclass
class OperationResult(Result):
    id: str
    method: str
    path: str
    where: str
    summary: str
    description: str
    operation_id: str | None
    flags: list[str]
    parameters: list[ParameterRow]
    request: list[BodyRow]
    responses: list[BodyRow]
    # Any one entry is enough: {scheme: {"type": ..., "scopes": [...]}}, {} for anonymous access. None when the
    # spec declares no security for it.
    security: list[dict] | None = None
    # Requests the API sends back: name, expression (its URL), method, summary, responses, and `bodies`:
    # [{media_type, schema (node id), type}].
    callbacks: list[dict] = field(default_factory=list)
    # Links from a response to another operation: status, name, operationId or operationRef, parameters.
    links: list[dict] = field(default_factory=list)
    expanded: list[SchemaResult] = field(default_factory=list)  # with expand: its bodies' schemas
    omitted: list[str] = field(default_factory=list)  # ids of schemas expand left out to stay within max_chars

    def to_text(self, full: bool = False) -> str:
        return _with_expanded(self._text(full), self.expanded, self.omitted, full)

    def _text(self, full: bool) -> str:
        lines = [f"operation {self.method} {self.path} {_cite(self.id, self.where)}"]
        if self.operation_id:
            lines.append(f"operationId: {self.operation_id}")
        if self.security is None:
            lines.append("security: none declared")  # not "not shown": the spec says nothing about it
        else:
            ways = [" + ".join(f"{name} ({s['type']}" + (f"; scopes {', '.join(s['scopes'])}" if s["scopes"] else "")
                               + ")" for name, s in way.items()) or "anonymous" for way in self.security]
            lines.append("security: " + (" or ".join(ways) or "none required"))
        if self.flags:
            lines.append(", ".join(self.flags))
        if self.summary:
            lines.append(f"summary: {_cut(self.summary, MAIN_LIMIT, full)}")
        if self.description and self.description != self.summary:
            lines.append(f"description: {_cut(self.description, MAIN_LIMIT, full)}")
        lines.append(f"parameters ({len(self.parameters)}):" if self.parameters else "parameters: none")
        for p in self.parameters:
            parts = [_full_enum(p.type, p.enum, full, row=True)] + _limit_text(p.limits) + (["required"] if p.required
                                                                                   else []) + p.flags
            lines.append(f"  {p.location} {p.name}: {', '.join(parts)} {_cite(p.id, p.where)}"
                         + (f" — {_cut(p.description, ROW_LIMIT, full)}" if p.description else ""))
        for b in self.request:
            lines.append(f"request body{' (required)' if b.required else ''}: {b.media_type}: {b.type} [{b.schema}]")
        lines.append("responses:" if self.responses else "responses: none with a schema")
        for b in self.responses:
            what = f"header {b.header}" if b.header else b.media_type
            lines.append(f"  {b.status} {what}: {b.type} [{b.schema}]")
            lines += _field_lines(b.fields, "    ", full)
        if self.links:
            lines.append("links:")
        for link in self.links:
            to = (f"operationId {link['operationId']}" if link.get("operationId") else
                  f"operationRef {link.get('operationRef')}")
            given = ", ".join(f"{k} = {v}" for k, v in (link.get("parameters") or {}).items())
            lines.append(f"  {link['status']} {link['name']} -> {to}" + (f" ({given})" if given else ""))
        if self.callbacks:
            lines.append("callbacks:")
        for cb in self.callbacks:
            lines.append(f"  {cb['name']}: {cb['method']} {cb['expression']}"
                         + (f" — {_cut(cb['summary'], ROW_LIMIT, full)}" if cb.get("summary") else ""))
            lines += [f"    sends {b['media_type']}: {b['type']} [{b['schema']}]" for b in cb["bodies"]]
        return "\n".join(lines)


def get_operation(spec, method: str, path: str | None = None, expand: int = 0, errors: bool = False,
                  max_chars: int | None = None) -> OperationResult:
    """`get_operation(spec, "GET", "/pets/{id}")`, or one string: `"GET /pets/{id}"`, an operationId, an id.
    `expand=1` also gives the named schemas of its request body and of its responses other than errors (4xx,
    5xx, default), each as a result of its own; `errors=True` those of errors too. `max_chars` caps the text as
    for get_schema."""
    graph = spec.graph
    name = f"{method.upper()} {path}" if path is not None else method
    if path is None:
        parts = method.split(None, 1)
        if len(parts) == 2 and parts[0].upper() in METHODS:
            name = f"{parts[0].upper()} {parts[1]}"
    node = _one(spec, g.OPERATION, name, "operation", plain=False)
    parameters = []
    for e in graph.outgoing(node.id, g.TAKES):
        p = graph.nodes[e.target]
        parameters.append(ParameterRow(location=e.data["in"], name=e.data["name"], id=p.id,
                                       type=describe(graph, p, name_refs=True), enum=enum_values(graph, p),
                                       required=bool(p.data.get("required")), where=p.where(),
                                       description=full_text(p.data.get("description")), flags=_flags(p.data),
                                       limits=limits(graph, p)))
    request = [BodyRow(media_type=e.data.get("media_type"), schema=e.target,
                       type=describe(graph, graph.nodes[e.target], 1), required=bool(e.data.get("required")))
               for e in graph.outgoing(node.id, g.ACCEPTS)]
    responses = []
    for e in graph.outgoing(node.id, g.RETURNS):
        target = graph.nodes[e.target]
        status = e.data.get("status", "")
        body = BodyRow(media_type=e.data.get("media_type"), schema=e.target, type=describe(graph, target, 1),
                       status=status, header=e.data.get("header"))
        if not body.header and status.startswith("2"):
            body.fields = field_rows(graph, target)
        responses.append(body)
    d = node.data
    callbacks = []
    for cb in d.get("callbacks", []):
        sent = [{"media_type": e.data.get("media_type"), "schema": e.target,
                 "type": describe(graph, graph.nodes[e.target], 1)}
                for e in graph.outgoing(node.id, g.CALLBACK)
                if e.data.get("callback") == cb["name"] and e.data.get("method") == cb["method"]]
        callbacks.append({**cb, "bodies": sent})
    bodies = request + [b for b in responses if errors or str(b.status)[:1] in ("1", "2", "3")]
    return _fit(OperationResult(id=node.id, method=d["method"], path=d["path"], where=node.where(),
                           summary=full_text(d.get("summary")), description=full_text(d.get("description")),
                           operation_id=d.get("operationId"), flags=_flags(d), parameters=parameters,
                           request=request, responses=responses,
                           expanded=_expand(spec, [t for b in bodies if not b.header
                                                   for t in _named_targets(graph, b.schema)], expand, set())
                           if expand > 0 else [],
                           callbacks=callbacks, links=list(d.get("links", [])),
                           security=None if d.get("security") is None else
                           [{name: {"type": graph.security_schemes.get(name, {}).get("type", "undefined"),
                                    "scopes": scopes} for name, scopes in way.items()} for way in d["security"]]),
                max_chars)


# ---- find_field ---------------------------------------------------------------------------------

@dataclass
class HolderRow:
    schema: str
    inherited: bool
    required: bool
    via: list[str]


@dataclass
class UseRow:
    operation: str
    role: str  # "response 200", "request body", "parameter"
    through: str  # the schema or field the operation reaches it by
    direct: bool


@dataclass
class FieldResult(Result):
    name: str
    definitions: list[FieldRow]
    holders: list[HolderRow]
    used_by: list[UseRow]

    def to_text(self, full: bool = False) -> str:
        n = len(self.definitions)
        lines = [f"field {self.name}: written in {n} place{'s' * (n != 1)}"]
        for d in self.definitions:
            owner = d.id.split("/", 1)[1].rsplit(".", 1)[0]
            lines.append(f"  in {owner}: {d.type} {_cite(d.id, d.where)}" + (f" — {_cut(d.description, ROW_LIMIT, full)}"
                                                                                if d.description else ""))
        lines.append(f"held by {len(self.holders)} schema{'s' * (len(self.holders) != 1)}:")
        for h in self.holders:
            how = "inherited via " + " > ".join(v.split("/", 1)[1] for v in h.via) if h.inherited else "own"
            lines.append(f"  {h.schema.split('/', 1)[1]} ({how}{', required' if h.required else ''}) [{h.schema}]")
        if self.used_by:
            lines.append(f"used by {len(set(u.operation for u in self.used_by))} operations:")
            for u in self.used_by[:30]:
                lines.append(f"  {u.operation.split('/', 1)[1]}: {u.role}, through {u.through.split('/', 1)[1]}")
            if len(self.used_by) > 30:
                lines.append(f"  … and {len(self.used_by) - 30} more")
        else:
            lines.append("used by no operation")
        return "\n".join(lines)


def _role(u: dict) -> str:
    if u["edge"] == g.RETURNS:
        return f"response {u.get('status')}" + (f" header {u['header']}" if u.get("header") else "")
    if u["edge"] == g.ACCEPTS:
        return "request body"
    if u["edge"] == g.CALLBACK:
        return f"callback {u.get('callback')} body"
    return f"parameter {u.get('name', '')}".strip()


def find_field(spec, name: str) -> FieldResult:
    """Where a field is written, which schemas hold it (own or inherited), and which operations use them.
    `name` is a field name (`weight`) or a schema and a field (`Dog.weight`, which follows inheritance)."""
    graph, index = spec.graph, spec.index
    ids = graph.resolve(name) or graph.resolve(f"{spec.label}:{g.FIELD}/{name}")
    if not ids and "." in name:
        schema_name, field_name = name.rsplit(".", 1)
        schema = _one(spec, g.SCHEMA, schema_name, "schema")
        ids = graph.resolve(f"{spec.label}:{g.FIELD}/{schema.name}.{field_name}")
    if not ids:
        ids, _ = index.fields.lookup(name)
    if not ids:
        raise NotFound("field", name, index.fields.suggest(name))
    definitions, holders, uses = [], {}, {}
    for fid in ids:
        f = graph.nodes[fid]
        definitions.append(FieldRow(name=f.data["name"], id=f.id, type=describe(graph, f, name_refs=True),
                                    required=bool(f.data.get("required")),  # in the schema it is written in
                                    where=f.where(), description=full_text(f.data.get("description")),
                                    flags=_flags(f.data), limits=limits(graph, f), enum=enum_values(graph, f)))
        for e in graph.incoming(fid, g.HAS_FIELD):
            holders.setdefault(e.source, HolderRow(schema=e.source, inherited=e.data["inherited"],
                                                   required=e.data["required"], via=list(e.data["via"])))
        for u in index.used_by(fid):
            row = UseRow(operation=u["operation"], role=_role(u), through=u["through"], direct=u["direct"])
            uses.setdefault((row.operation, row.role), row)
    names = sorted({d.name for d in definitions})
    return FieldResult(name=", ".join(names), definitions=definitions,
                       holders=sorted(holders.values(), key=lambda h: (h.inherited, h.schema)),
                       used_by=sorted(uses.values(), key=lambda u: (u.operation, u.role)))


# ---- list_operations ----------------------------------------------------------------------------

@dataclass
class OperationRow:
    id: str
    method: str
    path: str
    summary: str
    where: str
    flags: list[str] = field(default_factory=list)


@dataclass
class OperationList(Result):
    label: str
    filter: str | None
    operations: list[OperationRow]

    def to_text(self, full: bool = False) -> str:
        head = f"{len(self.operations)} operation{'s' * (len(self.operations) != 1)} in {self.label}"
        head += f" matching {self.filter!r}" if self.filter else ""
        rows = [f"  {o.method} {o.path}" + (f" — {_cut(o.summary, ROW_LIMIT, full)}" if o.summary else "")
                + (f" ({', '.join(o.flags)})" if o.flags else "") for o in self.operations]
        return "\n".join([head + ":"] + rows) if rows else head


def list_operations(spec, filter: str | None = None, method: str | None = None, tag: str | None = None) -> OperationList:
    """Every operation, or those whose method, path, summary, operationId or tags contain `filter` (any case)."""
    rows = []
    for node in spec.graph.of_kind(g.OPERATION):
        d = node.data
        if method and d["method"] != method.upper():
            continue
        if tag and tag not in (d.get("tags") or []):
            continue
        haystack = " ".join(str(x) for x in (d["method"], d["path"], d.get("summary"), d.get("operationId"),
                                             *(d.get("tags") or [])) if x).lower()
        if filter and filter.lower() not in haystack:
            continue
        rows.append(OperationRow(id=node.id, method=d["method"], path=d["path"],
                                 summary=full_text(d.get("summary")), where=node.where(), flags=_flags(d)))
    rows.sort(key=lambda r: (r.path, METHODS.index(r.method) if r.method in METHODS else 99))
    return OperationList(label=spec.label, filter=filter, operations=rows)


# ---- search -------------------------------------------------------------------------------------

@dataclass
class Hit:
    id: str
    kind: str
    name: str
    score: float
    where: str
    text: str


@dataclass
class SearchResult(Result):
    query: str
    hits: list[Hit]

    def to_text(self, full: bool = False) -> str:
        if not self.hits:
            return f"no matches for {self.query!r}"
        lines = [f"{len(self.hits)} matches for {self.query!r}:"]
        for h in self.hits:
            lines.append(f"  {h.kind} {h.name} {_cite(h.id, h.where)}" + (f" — {_cut(h.text, ROW_LIMIT, full)}" if h.text else ""))
        return "\n".join(lines)


def search(spec, text: str, kinds=None, limit: int = 10) -> SearchResult:
    """Keyword search (BM25) over names and descriptions. Ranked, not exact: use it when no name is known."""
    hits = []
    for node_id, score in spec.index.keywords.search(text, kinds, limit):
        n = spec.graph.nodes[node_id]
        about = n.data.get("summary") or n.data.get("description") or n.data.get("title")
        hits.append(Hit(id=n.id, kind=n.kind, name=n.name, score=round(score, 3), where=n.where(),
                        text=full_text(about)))
    return SearchResult(query=text, hits=hits)


# ---- source -------------------------------------------------------------------------------------

@dataclass
class SourceResult(Result):
    id: str
    where: str
    text: str

    def to_text(self, full: bool = False) -> str:
        return f"{self.id} {self.where}\n{self.text}"


def source(spec, node_id: str) -> SourceResult:
    if node_id not in spec.graph.nodes:
        ids = spec.graph.resolve(node_id)
        if len(ids) != 1:
            raise NotFound("node", node_id) if not ids else Ambiguous("node", node_id, ids)
        node_id = ids[0]
    node = spec.graph.nodes[node_id]
    return SourceResult(id=node.id, where=node.where(), text=spec.source(node.id))


# ---- names in a question ------------------------------------------------------------------------

@dataclass
class Mention:
    text: str  # as it appears in the question
    kind: str
    ids: list[str]
    how: str  # "identifier", "plain words", "path", "path segment", "field of X"
    name: str = ""  # the name it resolved to: billingAddress for "billing address" (several: joined with ", ")
    # How it was written, the surest first: "exact" (a path, a name shaped like code, or quoted), "phrase" (two
    # or more plain words), "capitalised" (one capitalised word: "Statement", which usually means the schema,
    # or the first word of a sentence), "word" (one lowercase word, which may be an everyday word that only
    # happens to be a name). The caller decides how far to trust each.
    strength: str = ""


@dataclass
class Mentions(Result):
    text: str
    mentions: list[Mention]

    def to_text(self, full: bool = False) -> str:
        if not self.mentions:
            return "no names from the spec found"
        return "\n".join(f"{m.text!r} -> {m.kind} {', '.join(m.ids)} ({m.how})" for m in self.mentions)


_PATH = re.compile(rf"(?:\b(GET|PUT|POST|DELETE|OPTIONS|HEAD|PATCH|TRACE)\s+)?({PATH})", re.I)
_TOKEN = re.compile(r"`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*)")
_NUMBER = re.compile(r"(?<![\w.])\d+(?![\w.])")


# A method, one to three words and a word that says it is an operation: "Get pets endpoint", "the post order call".
_VERB_NOUN = re.compile(r"\b(get|put|post|delete|patch)\s+((?:[A-Za-z]+\s+){0,2}?[A-Za-z]+)\s+"
                        r"(?:endpoint|operation|route|call|request|api)s?\b", re.I)


def _segments(path: str) -> list[str]:
    return [p for p in path.strip("/").split("/") if p]


def _loose_path(path: str, method: str | None, operations: dict) -> list[str]:
    """Operations a path written loosely means: braces left out or a value in their place ("/pets/petId",
    "/pets/42" for /pets/{petId}), any case. Each other segment must match. The closest ones: a segment written
    as its parameter's name counts over one that is any value."""
    written = _segments(path.rstrip(".,;:?!"))
    best, out = -1, []
    for (m, p), i in operations.items():
        if method and m != method.upper():
            continue
        segs = _segments(p)
        if len(segs) != len(written) or all(seg.startswith("{") for seg in segs):
            continue  # a path of templates only would match any text: "</div>" for /{format}
        score = 0
        for w, seg in zip(written, segs):
            if seg.startswith("{") and seg.endswith("}"):
                score += w.strip("{}").lower() == seg[1:-1].lower()
            elif w.lower() != seg.lower():
                break
        else:
            if score > best:
                best, out = score, [i]
            elif score == best:
                out.append(i)
    return out


def _named_collection(method: str, noun: str, operations: dict) -> list[str]:
    """Operations named by a method and words for their last fixed segment: "get pets" -> GET /pets, "get pet"
    -> GET /pets/{petId}. The shortest such paths, so /pets wins over /owners/{id}/pets."""
    said = key(noun)
    found = []
    for (m, p), i in operations.items():
        if m != method.upper():
            continue
        segs = _segments(p)
        item = bool(segs) and segs[-1].startswith("{")  # /pets/{petId}, against /pets
        named = segs[-2] if item and len(segs) > 1 else None if item else segs[-1] if segs else None
        if not named or named.startswith("{"):
            continue
        last = key(named)
        if (not item and said == last) or (item and said != last and said in variants(last)):
            found.append((len(segs), i))
    shortest = min((n for n, _ in found), default=0)
    return [i for n, i in found if n == shortest]


_ARTICLES = {"a", "an", "the"}
_DETERMINERS = _ARTICLES | {"this", "that", "these", "those", "my", "your", "our", "their", "its", "his", "her",
                            "each", "every", "any", "some", "no", "all", "one"}
_POSSESSIVE = re.compile(r"['\u2019]s\b")  # port's
_PLURAL_POSSESSIVE = re.compile(r"['\u2019](?![A-Za-z0-9])")  # ports'
_BREAK = re.compile(r"[.,;:!?()] ")  # words either side of it are not one phrase: "created. A template"
_SAME_PHRASE = re.compile(r"(['\u2019]s?)?[ \t]+")  # only spaces, or a possessive, between two words
_PARAMETER_NEXT = re.compile(r"\s+(?:(?:query|path|cookie|request|http)\s+)?(?:header|parameter|param)s?\b", re.I)


def names_in(spec, text: str) -> Mentions:
    """The spec's names a question mentions, without guessing.

    - a path, with or without its method: "GET /pets/{id}";
    - an identifier (`PetOwner`, `petId`, `listPets`), as written or in another naming style (`PetOwner` finds
      `pet_owner`); a name that is both a field and a parameter is both;
    - a name of two words or more, or one quoted word naming nothing else, that is the last fixed segment of a
      path names the operations there: `estimateShipping` -> POST /estimateShipping (not when it names a
      schema or operation already); so do plain words spelling it, also with "a", "an" or "the" between:
      "estimate the shipping" -> POST /estimateShipping;
    - plain words spelling an operationId of two words or more, also with "a", "an" or "the" between and the first
      word (the verb) in another form: "book a shipment", "booking a shipment" -> bookShipment. Beside what the
      words also name, never instead of it;
    - two to four plain words naming a schema: "pet owners" -> PetOwner;
    - a single plain word naming a schema, when the word names a thing in this spec: no field has that name, or
      the fields that do hold that schema. `subscription` counts where a `subscription` field holds a
      Subscription; `status` does not where `status` fields hold plain strings;
    - two to four plain words naming a field whose name has as many words: "billing address" -> billingAddress,
      in every schema that has it, or only in the schemas the question mentions when they have it;
    - a plain word that is a field of a schema mentioned in the same question: "the amount of a refund"."""
    graph, names = spec.graph, spec.index.names
    found: list[tuple[int, int, Mention]] = []
    operations = {(n.data["method"], n.data["path"]): n.id for n in graph.of_kind(g.OPERATION)}
    paths: dict[str, list[str]] = {}
    for (m, p), i in operations.items():
        paths.setdefault(p, []).append(i)
    for match in _PATH.finditer(text):
        method, path = match.group(1), trim_path(match.group(2))
        how = "path"
        if method and (method.upper(), path) in operations:
            ids = [operations[(method.upper(), path)]]
        else:
            ids = paths.get(path, [])
            if ids and method:  # "delete /pets/{petId}" where only GET is: what the path has, said so
                how = "path (other method)"
        if not ids and path.strip("/"):
            ids, how = _loose_path(path, method, operations), "path (loose)"
            if not ids and method:
                ids, how = _loose_path(path, None, operations), "path (other method)"
        if ids:
            written = match.group(0)[:len(match.group(0)) - (len(match.group(2)) - len(path))]
            found.append((match.start(), match.start() + len(written), Mention(
                written.strip(), g.OPERATION, ids, how, strength="exact" if how == "path" else "phrase")))
    for match in _VERB_NOUN.finditer(text):  # "the get pets endpoint" -> GET /pets
        if any(s < match.end() and match.start() < e for s, e, _ in found):
            continue
        ids = _named_collection(match.group(1), match.group(2), operations)
        if ids:
            found.append((match.start(), match.end(), Mention(match.group(0), g.OPERATION, ids, "path (loose)",
                                                              strength="phrase")))
    tokens = [(m.start(), m.end(), m.group(1) or m.group(2), m.group(1) is not None) for m in _TOKEN.finditer(text)]

    def taken(start, end):
        return any(s < end and start < e for s, e, _ in found)

    by_segment: dict[str, list[str]] = {}

    def segment_ops():  # operations by the last fixed segment of their path, built on first use
        if not by_segment:
            for (m, p), i in operations.items():
                fixed = [s for s in p.split("/") if s and "{" not in s]
                if fixed and not p.startswith("webhook:"):
                    by_segment.setdefault(fixed[-1], []).append(i)
            by_segment.setdefault("", [])
        return by_segment

    # Identifiers as written come first: they are the most certain.
    for start, end, word, quoted in tokens:
        if taken(start, end) or (word.islower() and word.isalpha() and not quoted):
            continue
        # Shaped like code (PetOwner, petId, pet_id), or quoted; "Pets" may only start a sentence, "API" is prose.
        sure = "exact" if quoted or not re.fullmatch(r"[A-Z]?[a-z]+|[A-Z]+s?", word) else "word"
        hit = False
        for kind in (g.SCHEMA, g.OPERATION, g.FIELD, g.PARAMETER):
            if kind == g.FIELD:
                ids = spec.index.fields.by_name.get(word)
            elif kind == g.PARAMETER:
                ids = [n.id for n in graph.of_kind(g.PARAMETER) if n.data.get("name") == word]
            else:
                ids = names.exact[kind].get(word)
            if ids:
                found.append((start, end, Mention(word, kind, list(dict.fromkeys(ids)), "identifier",
                                                  strength=sure)))
                hit = True
                if kind != g.FIELD:  # a field's name may also be a parameter's: both are named
                    break
        if not hit:
            for kind in (g.SCHEMA, g.OPERATION):
                ids, how = names.lookup(kind, word)
                if ids and how != "exact":
                    found.append((start, end, Mention(word, kind, ids, "identifier", strength=sure)))
                    break
        # A name of two words or more (estimateShipping, pet_owners) that is the last fixed segment of a path
        # names the operations there, unless it names a schema or operation already: "the estimateShipping call"
        # -> POST /estimateShipping. One quoted word does when it names nothing else ("`captures`" -> POST
        # /orders/{id}/captures; "when `secret` is null" is the field). A version segment ("v1") is not a name.
        several = len([w for w in words(word) if w.isalpha() and len(w) > 1]) > 1
        if (several or quoted and not any(e > start and s < end for s, e, _ in found)) and not any(
                e > start and s < end and m.kind in (g.SCHEMA, g.OPERATION) for s, e, m in found):
            ids = segment_ops().get(word)
            if ids:
                found.append((start, end, Mention(word, g.OPERATION, ids, "path segment", strength=sure)))
    # Then the longest run of plain words that names a schema or a field of two words or more ("billing
    # address" -> billingAddress); words naming both are both.
    param_plain: dict[str, list[str]] = {}  # parameters by plain words; X-Request-Id also by "request id"
    param_words = []  # (id, words of its name), for "the actor type header" -> Api-Actor-Type
    for n in graph.of_kind(g.PARAMETER):
        pname = str(n.data.get("name") or "")
        param_words.append((n.id, words(pname)))
        for written in {pname} | ({pname[2:]} if pname[:2].lower() == "x-" else set()):
            if len(words(written)) > 1:  # the windows below are two words or more
                param_plain.setdefault(key(written), []).append(n.id)
    words_only = sorted([(s, e, w) for s, e, w, q in tokens if not q] +  # with numbers: "address line 2"
                        [(m.start(), m.end(), m.group(0)) for m in _NUMBER.finditer(text)])
    for size in (4, 3, 2):
        for i in range(len(words_only) - size + 1):
            window = words_only[i:i + size]
            start, end = window[0][0], window[-1][1]
            if taken(start, end) or "\n" in text[start:end]:
                continue
            phrase = " ".join(w for _, _, w in window)
            if _PARAMETER_NEXT.match(text, end):  # "the request id header": the parameter, not its type
                said = words(phrase)
                pids = param_plain.get(key(phrase)) or [
                    i for i, w in param_words if len(w) > len(said) and w[-len(said):] == said]
                if pids:
                    found.append((start, end, Mention(text[start:end], g.PARAMETER, list(dict.fromkeys(pids)),
                                                      "plain words")))
                    continue
            ids, how = names.lookup(g.SCHEMA, phrase)
            if ids and how == "plain words":
                found.append((start, end, Mention(text[start:end], g.SCHEMA, ids, "plain words")))
            fields = [i for v in sorted(variants(key(phrase))) for i in spec.index.fields.plain.get(v, ())]
            if fields:
                found.append((start, end, Mention(text[start:end], g.FIELD, list(dict.fromkeys(fields)),
                                                  "plain words")))
            elif not ids and key(phrase) in param_plain:
                found.append((start, end, Mention(text[start:end], g.PARAMETER, param_plain[key(phrase)],
                                                  "plain words")))
    # Plain words that spell a path's last fixed segment of two words or more name the operations there, also
    # with "a", "an" or "the" between: "estimate the shipping" -> POST /estimateShipping. Beside what the words
    # also name: the request's schema is often named the same.
    spelled: dict[str, list[str]] = {}
    for seg, ids in segment_ops().items():
        if len([w for w in words(seg) if w.isalpha() and len(w) > 1]) > 1:
            spelled.setdefault(key(seg), []).extend(ids)
    # An operationId of two words or more, spelled in plain words the same way: "book a shipment", "booking a
    # shipment" -> bookShipment. The first word may be the verb in another form (booking, booked).
    by_id: dict[str, list[str]] = {}
    for n in graph.of_kind(g.OPERATION):
        op_id = str(n.data.get("operationId") or "")
        if len([w for w in words(op_id) if w.isalpha() and len(w) > 1]) > 1:
            by_id.setdefault(key(op_id), []).append(n.id)
    op_verbs = {words(str(n.data.get("operationId") or ""))[0].lower() for n in graph.of_kind(g.OPERATION)
                if len(words(str(n.data.get("operationId") or ""))) > 1}
    ops_at = [(s, e) for s, e, m in found if m.kind == g.OPERATION]
    spelled_found = []  # added last: they take no words from what the words also name
    for size in range(7, 1, -1):
        for i in range(len(words_only) - size + 1):
            window = words_only[i:i + size]
            start, end = window[0][0], window[-1][1]
            said = [w for _, _, w in window]
            if said[0].lower() in _ARTICLES or said[-1].lower() in _ARTICLES or "\n" in text[start:end] or \
                    _BREAK.search(text, start, end) or any(s < end and start < e for s, e in ops_at):
                continue
            kept = [w for w in said if w.lower() not in _ARTICLES]
            ids = spelled.get(key(" ".join(kept)))
            if ids:
                spelled_found.append((start, end, Mention(text[start:end], g.OPERATION, list(dict.fromkeys(ids)),
                                                          "path segment", strength="phrase")))
                ops_at.append((start, end))
    # Beside those, never instead of them: the longest run spelling an operationId, its verb in any form.
    by_id_at = [(s, e) for s, e, m in found if m.kind == g.OPERATION]
    for size in range(7, 1, -1):
        for i in range(len(words_only) - size + 1):
            window = words_only[i:i + size]
            start, end = window[0][0], window[-1][1]
            said = [w for _, _, w in window]
            if said[0].lower() in _ARTICLES or said[-1].lower() in _ARTICLES or "\n" in text[start:end] or \
                    _BREAK.search(text, start, end) or any(s < end and start < e for s, e in by_id_at):
                continue
            kept = [w for w in said if w.lower() not in _ARTICLES]
            before = words_only[i - 1][2].lower() if i and _SAME_PHRASE.fullmatch(text, words_only[i - 1][1], start) \
                else ""
            # "cancel a scheduled order", "the booked shipments": a verb form after a determiner, or after
            # another verb this spec's operations start with, describes a thing; it does not do the action.
            inflected = len(_verb_forms(kept[0])) > 1  # booking, booked: not the verb as written in the id
            describes = inflected and (before in _DETERMINERS or before in op_verbs)

            def spells(parts, describes=describes):
                if describes:
                    return by_id.get(key(" ".join(parts)))
                return next((by_id[k] for v in _verb_forms(parts[0]) if (k := key(" ".join([v, *parts[1:]]))) in by_id),
                            None)
            ids = None
            if _POSSESSIVE.match(text, end) or (said[-1].lower().endswith("s") and _PLURAL_POSSESSIVE.match(text, end)):
                # "getting a port's public profile": the last word belongs to the words after it, so the run
                # names an operationId only together with some of those: getPortProfile, never getPort.
                after, at, prev = [], i + size, end
                while at < len(words_only) and len(after) < 3:
                    s_, e_, w_ = words_only[at]
                    at += 1
                    if w_ == "s" and s_ == prev + 1:  # the "s" of "port's"
                        continue
                    if not _SAME_PHRASE.fullmatch(text, prev, s_):
                        break
                    after.append((s_, e_, w_))
                    prev = e_
                for pick in sorted((c for n_ in range(len(after), 0, -1) for c in combinations(after, n_)),
                                   key=lambda c: (-len(c), c[-1][1])):
                    if ids := spells(kept + [w for _, _, w in pick]):
                        end = pick[-1][1]
                        break
            else:
                ids = spells(kept)
            if ids:
                spelled_found.append((start, end, Mention(text[start:end], g.OPERATION, list(dict.fromkeys(ids)),
                                                          "operationId", strength="phrase")))
                by_id_at.append((start, end))
    # Then a single plain word naming a thing, not an attribute.
    plain = [(s, e, w) for s, e, w, q in tokens if not q and w.islower() and w.isalpha()]
    for start, end, word in plain:
        if taken(start, end):
            continue
        ids, how = names.lookup(g.SCHEMA, word)
        if ids and _names_a_thing(spec, word, ids):
            found.append((start, end, Mention(word, g.SCHEMA, ids, "plain words")))
    # Last, fields of the schemas the question mentions: a run of plain words naming a field keeps only
    # theirs when they have it, and a plain word that is a field of one is that field.
    mentioned = [i for _, _, m in found if m.kind == g.SCHEMA for i in m.ids]
    for n, (start, end, m) in enumerate(found):
        if m.kind == g.FIELD and m.how == "plain words":
            owners = [i for i in mentioned if any(e.target in m.ids for e in graph.outgoing(i, g.HAS_FIELD))]
            if owners:
                held = {e.target for i in owners for e in graph.outgoing(i, g.HAS_FIELD)}
                names_ = ", ".join(dict.fromkeys(graph.nodes[i].name for i in owners))
                found[n] = (start, end, Mention(m.text, g.FIELD, [i for i in m.ids if i in held], f"field of {names_}"))
    for start, end, word in plain:
        if taken(start, end) or not mentioned:
            continue
        hits = []
        for schema_id in mentioned:
            for e in graph.outgoing(schema_id, g.HAS_FIELD):
                f = graph.nodes[e.target]
                if f.data["name"].lower() == word or key(f.data["name"]) in variants(word):
                    hits.append(f.id)
        if hits:
            owners = ", ".join(dict.fromkeys(graph.nodes[i].name for i in mentioned))
            found.append((start, end, Mention(word, g.FIELD, list(dict.fromkeys(hits)), f"field of {owners}")))
    found += spelled_found
    found.sort(key=lambda f: f[0])
    for _, _, m in found:
        nodes = [graph.nodes[i] for i in m.ids]
        m.name = ", ".join(dict.fromkeys(n.data.get("name", n.name) if n.kind in (g.FIELD, g.PARAMETER) else n.name
                                         for n in nodes))
        m.strength = m.strength or ("exact" if m.how == "path" else
                                    "phrase" if len(re.findall(r"[A-Za-z0-9]+", m.text)) > 1 else "word")
        if m.strength == "word" and m.text[:1].isupper():
            m.strength = "capitalised"
    return Mentions(text=text, mentions=[m for _, _, m in found])


def _verb_forms(word: str) -> list[str]:
    """A word, and the verb it may be a form of: booking, booked -> book; updating -> update; getting -> get."""
    w = word.lower()
    out = [w]
    for end in ("ing", "ed"):
        if w.endswith(end) and len(w) > len(end) + 2:
            stem = w[:-len(end)]
            out += [stem, stem + "e"] + ([stem[:-1]] if len(stem) > 2 and stem[-1] == stem[-2] else [])
    return out


def _names_a_thing(spec, word: str, schema_ids: list[str]) -> bool:
    """Whether a word that names a schema means that schema, not an attribute that happens to share the name:
    true if no field is called that, or if a field called that holds one of these schemas (directly, as array
    items, as an option, or through a parent)."""
    graph = spec.graph
    fields = [i for v in variants(word) for i in spec.index.fields.by_lower.get(v, ())]
    if not fields:
        return True
    wanted = set(schema_ids)
    for fid in fields:
        frontier = [e.target for e in graph.outgoing(fid, g.FIELD_TYPE)]
        for _ in range(3):
            if wanted & set(frontier):
                return True
            frontier = [e.target for n in frontier for e in graph.outgoing(n)
                        if e.kind in (g.ITEMS, g.OPTION, g.INHERITS)]
    return False


# ---- resolving a name ---------------------------------------------------------------------------

def _one(spec, kind: str, name: str, what: str, plain: bool = True) -> Node:
    graph, names = spec.graph, spec.index.names
    if name in graph.nodes and graph.nodes[name].kind == kind:
        return graph.nodes[name]
    full = f"{spec.label}:{kind}/{name}"
    if full in graph.nodes:
        return graph.nodes[full]
    ids, how = names.lookup(kind, name)
    if how == "plain words" and not plain:
        ids = []
    if not ids:
        raise NotFound(what, name, names.suggest(kind, name))
    if len(ids) > 1:
        named = [i for i in ids if graph.nodes[i].data.get("named")]
        if len(named) == 1:
            return graph.nodes[named[0]]
        raise Ambiguous(what, name, ids)
    return graph.nodes[ids[0]]
