"""Comparing two specs: two versions of an API, or a branch and its main line. Labels are any names.

Nodes are matched by name. A change is rated for the side it affects: removing a field breaks those who read
it (responses), adding a required one breaks those who send it (requests). When a schema is compared on its
own, the side comes from where the newer spec uses it; a schema used on both sides, or nowhere, gets the worse
rating.

Levels: "breaking" (existing clients can fail), "warning" (they might), "info" (they will not). They follow
the defaults of oasdiff, the established breaking-change checker, so the two rate a change the same way. Two of
them are a matter of policy that some API owners decide otherwise: a new enum value and a new anyOf/oneOf option
in a response are breaking, since a client that handles every known value can fail on a new one.
"""

from collections import deque
from dataclasses import dataclass, field, replace

from . import graph as g
from .graph import Graph, Node
from .query import NotFound, Result, _one, describe
from .text import key

BREAKING, WARNING, INFO = "breaking", "warning", "info"
_ORDER = {BREAKING: 0, WARNING: 1, INFO: 2}

# kind -> (level when it is in a request, level when it is in a response)
RULES = {
    "field-added": (INFO, INFO),
    "required-field-added": (BREAKING, INFO),
    "field-removed": (WARNING, INFO),  # an optional field: clients already cope with it missing
    "required-field-removed": (WARNING, BREAKING),
    "type-changed": (BREAKING, BREAKING),
    "type-widened": (INFO, BREAKING),  # integer -> number
    "type-narrowed": (BREAKING, INFO),  # number -> integer
    "format-changed": (BREAKING, BREAKING),
    "format-added": (BREAKING, INFO),  # a tightening: values that were valid may not be
    "format-removed": (INFO, BREAKING),
    "types-added": (INFO, BREAKING),  # string -> string | number
    "became-any": (INFO, BREAKING),  # a typed schema became `{}`, which allows anything
    "no-longer-any": (BREAKING, INFO),
    "types-removed": (BREAKING, INFO),
    "options-introduced": (BREAKING, BREAKING),  # restructured into oneOf/anyOf: what was valid may not be
    "options-introduced-compatible": (INFO, BREAKING),  # ...but one option accepts all the old one did
    "options-dropped": (WARNING, WARNING),
    "type-replaced": (INFO, INFO),  # another named schema; what that means is in the changes under it
    "const-changed": (BREAKING, BREAKING),
    "required-added": (BREAKING, INFO),
    "required-removed": (INFO, BREAKING),
    "enum-value-added": (INFO, BREAKING),
    "enum-value-removed": (BREAKING, INFO),
    "nullable-added": (INFO, BREAKING),
    "nullable-removed": (BREAKING, INFO),
    "constraint-tightened": (BREAKING, INFO),
    "constraint-loosened": (INFO, BREAKING),
    "pattern-added": (BREAKING, INFO),
    "pattern-removed": (INFO, WARNING),
    "pattern-changed": (WARNING, WARNING),
    "default-changed": (INFO, INFO),
    "deprecated": (INFO, INFO),
    "undeprecated": (INFO, INFO),
    "read-only-changed": (INFO, INFO),
    "write-only-changed": (INFO, INFO),
    "description-changed": (INFO, INFO),
    "option-added": (INFO, BREAKING),
    "option-removed": (BREAKING, INFO),
    # an option that was an object with one property holding X is now X itself (or the reverse): the JSON
    # sent and received changes shape
    "options-unwrapped": (BREAKING, BREAKING),
    "options-wrapped": (BREAKING, BREAKING),
    "discriminator-values-changed": (BREAKING, BREAKING),
    "parent-added": (INFO, INFO),
    "parent-removed": (INFO, INFO),
    "schema-added": (INFO, INFO),
    "schema-removed": (BREAKING, BREAKING),
    "possibly-renamed": (INFO, INFO),  # a hint from similar contents, never a fact
    "operation-added": (INFO, INFO),
    "operation-removed": (BREAKING, BREAKING),
    "deprecated-operation-removed": (INFO, INFO),  # the accepted way to remove one
    "parameter-added": (INFO, INFO),
    "required-parameter-added": (BREAKING, BREAKING),
    "parameter-removed": (WARNING, WARNING),
    "request-body-added": (INFO, INFO),
    "required-request-body-added": (BREAKING, BREAKING),
    "request-body-removed": (BREAKING, BREAKING),  # a client that sends one may now be refused
    "media-type-added": (INFO, INFO),
    "media-type-removed": (BREAKING, BREAKING),
    "response-added": (INFO, INFO),
    "success-response-removed": (BREAKING, BREAKING),
    "response-removed": (INFO, INFO),
    "header-added": (INFO, INFO),
    "header-removed": (INFO, INFO),  # whether a response header was required is not recorded
    "operation-id-changed": (INFO, INFO),  # the same HTTP call; code generated from the spec is renamed
    # A security requirement is a list of ways in, any one of which is enough.
    "anonymous-access-removed": (BREAKING, BREAKING),
    "anonymous-access-added": (INFO, INFO),
    "security-removed": (BREAKING, BREAKING),  # a way in that clients may be using
    "security-added": (INFO, INFO),  # another way in
    "scope-added": (BREAKING, BREAKING),  # a token without it is refused
    "scope-removed": (INFO, INFO),
    "security-scheme-changed": (BREAKING, BREAKING),  # its type, or where the credential goes
}
_WORDING = {
    "required-field-added": "required field added", "type-widened": "type widened", "type-narrowed": "type narrowed",
    "required-parameter-added": "required parameter added",
    "required-request-body-added": "required request body added",
    "success-response-removed": "success response removed",
    "options-introduced-compatible": "options introduced (one accepts all the old schema did)", "required-field-removed": "required field removed",
}
# keyword -> True if a larger value allows more
_LIMITS = {"maxLength": True, "maximum": True, "maxItems": True, "maxProperties": True,
           "minLength": False, "minimum": False, "minItems": False, "minProperties": False}


@dataclass
class Change:
    kind: str
    where: str  # the way from the compared thing to what changed: "owner.name", "response 200 application/json"
    before: str | None
    after: str | None
    level: str
    side: str  # "request", "response", "both", or "none" (not used by any operation)
    a: str | None = None  # node id in the older spec
    b: str | None = None  # node id in the newer spec
    note: str | None = None

    def line(self) -> str:
        what = _WORDING.get(self.kind, self.kind.replace("-", " "))
        text = f"{self.level}: {what}"
        if self.where:
            text += f": {self.where}"
        if self.kind in _PAIRED:  # `before` lists the pairs itself: "{circle: Circle} -> Circle, ..."
            text += f" ({self.before})"
        elif self.before is not None or self.after is not None:
            text += f" ({_show(self.before)} -> {_show(self.after)})"
        node = self.b or self.a
        return text + (f" [{node}]" if node else "") + (f" — {self.note}" if self.note else "")


_PAIRED = {"options-unwrapped", "options-wrapped"}


def _show(v) -> str:
    return "none" if v is None else str(v)


def _level(kind: str, side: str) -> str:
    request, response = RULES[kind]
    if side == "request":
        return request
    if side == "response":
        return response
    return min(request, response, key=_ORDER.get)


@dataclass
class Comparison(Result):
    what: str  # "schema", "operation", "field"
    name: str
    a_label: str
    b_label: str
    a: str | None  # node id in the older spec, None if it is only in the newer
    b: str | None
    side: str
    changes: list[Change]

    def counts(self) -> dict[str, int]:
        return {level: sum(c.level == level for c in self.changes) for level in (BREAKING, WARNING, INFO)}

    def to_text(self, full: bool = False) -> str:
        head = f"compare {self.what} {self.name}: {self.a_label} -> {self.b_label}"
        lines = [head + f" [{self.a or 'absent'} -> {self.b or 'absent'}]"]
        if self.what == "schema":
            lines.append(f"used in: {_SIDES[self.side]}")
        if not self.changes:
            lines.append("no changes")
            return "\n".join(lines)
        c = self.counts()
        grouped = _grouped(sorted(self.changes, key=lambda ch: _ORDER[ch.level]))
        distinct = f", {len(grouped)} distinct" if len(grouped) != len(self.changes) else ""
        lines.append(f"{len(self.changes)} change{'s' * (len(self.changes) != 1)}{distinct} ({c[BREAKING]} breaking, "
                     f"{c[WARNING]} warning, {c[INFO]} info):")
        lines += [f"  {line}" for line in grouped]
        return "\n".join(lines)


def _grouped(changes: list[Change]) -> list[str]:
    """One line per distinct change. The same change reached by many ways (a field every subtype inherits) is
    shown once, with the first way and how many there are."""
    groups: dict[tuple, list[Change]] = {}
    for c in changes:
        groups.setdefault((c.kind, c.level, c.before, c.after, c.a, c.b), []).append(c)
    return [cs[0].line() + (f" (and {len(cs) - 1} more place{'s' * (len(cs) != 2)})" if len(cs) > 1 else "")
            for cs in groups.values()]


_SIDES = {"request": "requests", "response": "responses", "both": "requests and responses",
          "none": "no operation"}


# ---- the comparer -------------------------------------------------------------------------------
#
# Each pair of matching schemas is compared once, for its own changes and the pairs below it (a field's type,
# array items, options). What an operation or a schema reaches is then a walk over these pairs with a visited
# set, so cycles need no special care, and a change is rated for the side of whoever reaches it.

class _Comparer:
    def __init__(self, a, b):
        self.a, self.b = a, b
        self.ga: Graph = a.graph
        self.gb: Graph = b.graph
        self.pairs: dict[tuple, tuple[list[Change], list[tuple[str, tuple]]]] = {}
        self.queue = deque()

    def change(self, kind, where, before, after, side=None, a=None, b=None) -> Change:
        # The level and side are set when the change is reported, by whoever reaches it.
        return Change(kind, where, None if before is None else str(before), None if after is None else str(after),
                      "", "", a, b)

    # values written on a schema, field or parameter
    def values(self, da: dict, db: dict, where: str, side: str, a=None, b=None) -> list[Change]:
        out = []
        ta, tb = _types(da), _types(db)
        # A type is compared only when both sides state one: "no type" usually means it comes from elsewhere
        # (a parent, a $ref), and is compared there.
        if ta and tb and ta != tb:
            kind = "type-changed"
            if ta == {"integer"} and tb == {"number"}:
                kind = "type-widened"
            elif ta == {"number"} and tb == {"integer"}:
                kind = "type-narrowed"
            out.append(self.change(kind, where, "|".join(sorted(ta)), "|".join(sorted(tb)), side, a, b))
        if _nullable(da) != _nullable(db):
            out.append(self.change("nullable-added" if _nullable(db) else "nullable-removed", where, None, None,
                                   side, a, b))
        fa, fb = da.get("format"), db.get("format")
        if fa != fb:
            kind = "format-added" if fa is None else "format-removed" if fb is None else "format-changed"
            out.append(self.change(kind, where, fa, fb, side, a, b))
        if "const" in da or "const" in db:
            if da.get("const") != db.get("const"):
                out.append(self.change("const-changed", where, da.get("const"), db.get("const"), side, a, b))
        ea, eb = da.get("enum"), db.get("enum")
        if isinstance(ea, list) and isinstance(eb, list):
            for v in [v for v in eb if v not in ea]:
                out.append(self.change("enum-value-added", where, None, repr(v), side, a, b))
            for v in [v for v in ea if v not in eb]:
                out.append(self.change("enum-value-removed", where, repr(v), None, side, a, b))
        elif isinstance(eb, list) and ea is None:
            out.append(self.change("constraint-tightened", where, "any value", f"enum of {len(eb)}", side, a, b))
        elif isinstance(ea, list) and eb is None:
            out.append(self.change("constraint-loosened", where, f"enum of {len(ea)}", "any value", side, a, b))
        for k, larger_allows_more in _LIMITS.items():
            va, vb = da.get(k), db.get(k)
            if va == vb or not all(isinstance(v, (int, float)) or v is None for v in (va, vb)):
                continue
            if va is None:
                kind = "constraint-tightened"
            elif vb is None:
                kind = "constraint-loosened"
            else:
                kind = "constraint-loosened" if (vb > va) == larger_allows_more else "constraint-tightened"
            out.append(self.change(kind, where, f"{k} {_show(va)}", f"{k} {_show(vb)}", side, a, b))
        pa, pb = da.get("pattern"), db.get("pattern")
        if pa != pb:
            kind = "pattern-added" if pa is None else "pattern-removed" if pb is None else "pattern-changed"
            out.append(self.change(kind, where, pa, pb, side, a, b))
        if "default" in da and "default" in db and da["default"] != db["default"]:
            out.append(self.change("default-changed", where, da["default"], db["default"], side, a, b))
        if bool(da.get("deprecated")) != bool(db.get("deprecated")):
            out.append(self.change("deprecated" if db.get("deprecated") else "undeprecated", where, None, None,
                                   side, a, b))
        for k, kind in (("readOnly", "read-only-changed"), ("writeOnly", "write-only-changed")):
            if bool(da.get(k)) != bool(db.get(k)):
                out.append(self.change(kind, where, bool(da.get(k)), bool(db.get(k)), side, a, b))
        if (da.get("description") or "") != (db.get("description") or ""):
            out.append(self.change("description-changed", where, None, None, side, a, b))
        return out

    def pair(self, sa: Node, sb: Node) -> tuple:
        key = (sa.id, sb.id)
        if key not in self.pairs:
            self.pairs[key] = None
            self.queue.append((key, sa, sb))
        return key

    def build(self):
        while self.queue:
            key, sa, sb = self.queue.popleft()
            self.pairs[key] = self.local(sa, sb)

    def local(self, sa: Node, sb: Node):
        """(changes, children) for one pair of schemas: what they say themselves, their fields, and the pairs
        below them as (step, pair key)."""
        ga, gb = self.ga, self.gb
        out, kids = self.values(sa.data, sb.data, "", None, sa.id, sb.id), []
        any_a, any_b = _allows_anything(ga, sa), _allows_anything(gb, sb)
        if any_a != any_b:
            kind = "became-any" if any_b else "no-longer-any"
            out.append(self.change(kind, "", describe(ga, sa), describe(gb, sb), None, sa.id, sb.id))
        pa = {ga.nodes[e.target].name for e in ga.outgoing(sa.id, g.INHERITS)}
        pb = {gb.nodes[e.target].name for e in gb.outgoing(sb.id, g.INHERITS)}
        out += [self.change("parent-added", "", None, p, None, sa.id, sb.id) for p in sorted(pb - pa)]
        out += [self.change("parent-removed", "", p, None, None, sa.id, sb.id) for p in sorted(pa - pb)]
        fa, fb = _fields(ga, sa), _fields(gb, sb)
        options_a, options_b = ga.outgoing(sa.id, g.OPTION), gb.outgoing(sb.id, g.OPTION)
        if options_b and not options_a and not fb or options_a and not options_b and not fa:
            fa, fb = {}, {}  # restructured into (or out of) options: the options' comparison covers the fields
        for name in [n for n in fb if n not in fa]:
            f, edge = fb[name]
            kind = "required-field-added" if edge.data["required"] else "field-added"
            out.append(self.change(kind, name, None, describe(gb, f), None, None, f.id))
        for name in [n for n in fa if n not in fb]:
            f, edge = fa[name]
            kind = "required-field-removed" if edge.data["required"] else "field-removed"
            out.append(self.change(kind, name, describe(ga, f), None, None, f.id, None))
        for name in [n for n in fa if n in fb]:
            (f1, e1), (f2, e2) = fa[name], fb[name]
            if e1.data["required"] != e2.data["required"]:
                out.append(self.change("required-added" if e2.data["required"] else "required-removed", name,
                                       None, None, None, f1.id, f2.id))
            changes, children = self.typed(f1, f2, name, g.FIELD_TYPE)
            out += changes
            kids += children
        for kind, label in ((g.ITEMS, "[]"), (g.VALUES, "{}")):
            ia = {_edge_key(e): e for e in ga.outgoing(sa.id, kind)}
            ib = {_edge_key(e): e for e in gb.outgoing(sb.id, kind)}
            for k in [k for k in ia if k in ib]:
                step = label if k in ("items", "additionalProperties") else f"{label}{k}"
                out_, kids_ = self.matched(ga.nodes[ia[k].target], gb.nodes[ib[k].target], step)
                out += out_
                kids += kids_
        oa = {_option_key(ga, e): e for e in ga.outgoing(sa.id, g.OPTION)}
        ob = {_option_key(gb, e): e for e in gb.outgoing(sb.id, g.OPTION)}
        if ob and not oa:
            # Still accepting what it accepted if one option takes everything the old schema did.
            compatible = any(self.accepts_all(sa, gb.nodes[e.target]) for e in ob.values())
            kind = "options-introduced-compatible" if compatible else "options-introduced"
            out.append(self.change(kind, "", None, ", ".join(ob), None, sa.id, sb.id))
        elif oa and not ob:
            out.append(self.change("options-dropped", "", ", ".join(oa), None, None, sa.id, sb.id))
        else:
            out_, kids_, used_a, used_b = self.rewrapped(oa, ob)
            out += out_
            kids += kids_
            for k in [k for k in ob if k not in oa and k not in used_b]:
                out.append(self.change("option-added", "", None, k, None, sa.id, ob[k].target))
            for k in [k for k in oa if k not in ob and k not in used_a]:
                out.append(self.change("option-removed", "", k, None, None, oa[k].target, sb.id))
        for k in [k for k in oa if k in ob]:
            va, vb = sorted(oa[k].data.get("values", [])), sorted(ob[k].data.get("values", []))
            if va != vb:
                out.append(self.change("discriminator-values-changed", f"option {k}", ", ".join(va) or None,
                                       ", ".join(vb) or None, None, oa[k].target, ob[k].target))
            out_, kids_ = self.matched(ga.nodes[oa[k].target], gb.nodes[ob[k].target], f"option {k}")
            out += out_
            kids += kids_
        return out, kids

    def rewrapped(self, oa: dict, ob: dict):
        """Options that were a wrapper object around a schema ({circle: Circle}) and are now that schema itself
        (Circle), or the reverse: one change for all of them, not an option removed and another added. A wrapper
        around a schema that is gone pairs with the option it looks renamed to, and says so. The schemas inside
        are still compared."""
        out, kids, used_a, used_b = [], [], set(), set()
        for kind, wrappers, plain, graph in (("options-unwrapped", oa, ob, self.ga),
                                             ("options-wrapped", ob, oa, self.gb)):
            unwrapping = kind == "options-unwrapped"
            found = [(k, *inner) for k, e in wrappers.items()
                     if k not in plain and (inner := _wrapper_of(graph, e.target))]
            pairs, taken = [], set()  # (wrapper key, property, inner name, inner id, plain key, renamed)
            for k, prop, name, inner_id in found:
                if name in plain and name not in wrappers and name not in taken:
                    pairs.append((k, prop, name, inner_id, name, False))
                    taken.add(name)
            for k, prop, name, inner_id in found:  # then renames, among what is left
                if any(p[0] == k for p in pairs):
                    continue
                old_new = (name, self.renamed().get(name)) if unwrapping else \
                    next(((old, new) for old, new in self.renamed().items() if new == name), (None, None))
                key = old_new[1] if unwrapping else old_new[0]
                if key and key in plain and key not in wrappers and key not in taken:
                    pairs.append((k, prop, name, inner_id, key, True))
                    taken.add(key)
            if not pairs:
                continue
            shown = []
            for k, prop, name, inner_id, key, renamed in pairs:
                wrapper, note = f"{{{prop}: {name}}}", " (possibly renamed)" if renamed else ""
                if unwrapping:
                    used_a.add(k), used_b.add(key)
                    a_id, b_id = inner_id, plain[key].target
                    shown.append(f"{wrapper} -> {key}{note}")
                else:
                    used_b.add(k), used_a.add(key)
                    a_id, b_id = plain[key].target, inner_id
                    shown.append(f"{key} -> {wrapper}{note}")
                out_, kids_ = self.matched(self.ga.nodes[a_id], self.gb.nodes[b_id], f"option {name}")
                out += out_
                kids += kids_
            first, key = pairs[0][0], pairs[0][4]
            a, b = (wrappers[first].target, plain[key].target) if unwrapping else \
                (plain[key].target, wrappers[first].target)
            out.insert(0, self.change(kind, "", ", ".join(shown), None, None, a, b))
        return out, kids, used_a, used_b

    def renamed(self) -> dict[str, str]:
        """Named schemas only in the older spec -> the one only in the newer that looks like it renamed, as the
        possibly-renamed hints say. Worked out once."""
        if not hasattr(self, "_renamed"):
            named_a = {n.name: n for n in self.ga.of_kind(g.SCHEMA) if n.data.get("named")}
            named_b = {n.name: n for n in self.gb.of_kind(g.SCHEMA) if n.data.get("named")}
            gone = [n for name, n in named_a.items() if name not in named_b]
            new = [n for name, n in named_b.items() if name not in named_a]
            self._renamed = {old: n.name for old, (n, _) in _renames(self.ga, self.gb, gone, new).items()}
        return self._renamed

    def accepts_all(self, old: Node, option: Node) -> bool:
        """Whether `option` accepts every value `old` did: nothing between them breaks a request."""
        return not any(c.level == BREAKING for c in self.reach([("", self.pair(old, option))], "request"))

    def matched(self, sa: Node, sb: Node, where: str):
        """Two schemas in the same place. Another named schema is noted, then compared by what it holds:
        clients see the structure, not the name."""
        out = []
        if sa.data.get("named") and sb.data.get("named") and sa.name != sb.name:
            out.append(self.change("type-replaced", where, sa.name, sb.name, None, sa.id, sb.id))
        return out, [(where, self.pair(sa, sb))]

    def typed(self, na: Node, nb: Node, where: str, edge_kind: str):
        """A field or parameter: what it says itself, then what its type says, whether the type is written
        inline or is a schema of its own on either side."""
        ta = [e.target for e in self.ga.outgoing(na.id, edge_kind)]
        tb = [e.target for e in self.gb.outgoing(nb.id, edge_kind)]
        if ta and tb:
            out, kids = self.matched(self.ga.nodes[ta[0]], self.gb.nodes[tb[0]], where)
            return self.values(_annotations(na.data), _annotations(nb.data), where, None, na.id, nb.id) + out, kids
        if not ta and not tb:
            return self.values(na.data, nb.data, where, None, na.id, nb.id), []
        # Written inline on one side, a schema on the other: compare what each one is.
        sa = self.ga.nodes[ta[0]] if ta else None
        sb = self.gb.nodes[tb[0]] if tb else None
        da = {**(sa.data if sa else na.data), **_annotations(na.data)}
        db = {**(sb.data if sb else nb.data), **_annotations(nb.data)}
        out = self.values(da, db, where, None, na.id, nb.id)
        ea = _types(da) or (_option_types(self.ga, sa) if sa else set())
        eb = _types(db) or (_option_types(self.gb, sb) if sb else set())
        if ea and eb and ea != eb and not (_types(da) and _types(db)):
            # One side a single type, the other a choice of types: compare the sets.
            if eb - ea:
                out.append(self.change("types-added", where, "|".join(sorted(ea)), "|".join(sorted(eb)), None,
                                       na.id, nb.id))
            if ea - eb:
                out.append(self.change("types-removed", where, "|".join(sorted(ea)), "|".join(sorted(eb)), None,
                                       na.id, nb.id))
            return out, []
        if bool(sa) and _structured(self.ga, sa) or bool(sb) and _structured(self.gb, sb):
            if _types(da) and _types(da) == _types(db) or not (_types(da) and _types(db)):
                # The same type, its parts listed on one side only: say which parts appeared or went.
                for name, (f, _) in (_fields(self.gb, sb) if sb else {}).items():
                    out.append(self.change("field-added", _join(where, name), None, describe(self.gb, f), None,
                                           None, f.id))
                for name, (f, _) in (_fields(self.ga, sa) if sa else {}).items():
                    out.append(self.change("field-removed", _join(where, name), describe(self.ga, f), None, None,
                                           f.id, None))
            else:
                out.append(self.change("type-changed", where, describe(self.ga, na), describe(self.gb, nb), None,
                                       na.id, nb.id))
        return out, []

    def reach(self, roots: list[tuple[str, tuple]], side: str) -> list[Change]:
        """Every change in the pairs reachable from `roots` ((where, pair key)), rated for `side`, each
        `where` the shortest way from a root."""
        self.build()
        out, seen, queue = [], set(), deque(roots)
        while queue:
            where, key = queue.popleft()
            if key in seen:
                continue
            seen.add(key)
            if self.pairs[key] is None:
                continue  # being compared further up: its changes are reported there
            changes, children = self.pairs[key]
            out += [_rated(c, side, _join(where, c.where)) for c in changes]
            queue.extend((_join(where, step), child) for step, child in children)
        return out

    def operations(self, oa: Node, ob: Node) -> list[Change]:
        ga, gb = self.ga, self.gb
        out: list[Change] = []
        request_roots: list[tuple[str, tuple]] = []
        response_roots: list[tuple[str, tuple]] = []

        def own(change, side):
            out.append(_rated(change, side, change.where))

        if oa.data.get("operationId") != ob.data.get("operationId"):
            own(self.change("operation-id-changed", "operationId", oa.data.get("operationId"),
                            ob.data.get("operationId"), None, oa.id, ob.id), "request")
        for c in self.security(oa, ob):
            own(c, "request")
        if bool(oa.data.get("deprecated")) != bool(ob.data.get("deprecated")):
            own(self.change("deprecated" if ob.data.get("deprecated") else "undeprecated", "", None, None, None,
                            oa.id, ob.id), "request")
        pa = {(e.data["in"], e.data["name"]): ga.nodes[e.target] for e in ga.outgoing(oa.id, g.TAKES)}
        pb = {(e.data["in"], e.data["name"]): gb.nodes[e.target] for e in gb.outgoing(ob.id, g.TAKES)}
        for k in [k for k in pb if k not in pa]:
            kind = "required-parameter-added" if pb[k].data.get("required") else "parameter-added"
            own(self.change(kind, f"parameter {k[0]} {k[1]}", None, describe(gb, pb[k]), None, None, pb[k].id),
                "request")
        for k in [k for k in pa if k not in pb]:
            own(self.change("parameter-removed", f"parameter {k[0]} {k[1]}", describe(ga, pa[k]), None, None,
                            pa[k].id, None), "request")
        for k in [k for k in pa if k in pb]:
            where = f"parameter {k[0]} {k[1]}"
            if bool(pa[k].data.get("required")) != bool(pb[k].data.get("required")):
                own(self.change("required-added" if pb[k].data.get("required") else "required-removed", where,
                                None, None, None, pa[k].id, pb[k].id), "request")
            changes, children = self.typed(pa[k], pb[k], where, g.PARAM_TYPE)
            for c in changes:
                if c.kind == "type-changed" and c.after == "string" and c.before in ("integer", "number", "boolean"):
                    # A path, query, header or cookie value is text: every value that was valid still is.
                    c = replace(c, kind="type-widened")
                own(c, "request")
            request_roots += children
        ra = {e.data.get("media_type"): e for e in ga.outgoing(oa.id, g.ACCEPTS)}
        rb = {e.data.get("media_type"): e for e in gb.outgoing(ob.id, g.ACCEPTS)}
        if rb and not ra:
            required = any(e.data.get("required") for e in rb.values())
            own(self.change("required-request-body-added" if required else "request-body-added", "request body",
                            None, ", ".join(rb), None, None, ob.id), "request")
        elif ra and not rb:
            own(self.change("request-body-removed", "request body", ", ".join(ra), None, None, oa.id, None),
                "request")
        else:
            if any(e.data.get("required") for e in rb.values()) and not any(e.data.get("required")
                                                                              for e in ra.values()):
                own(self.change("required-added", "request body", None, None, None, oa.id, ob.id), "request")
            for mt in [m for m in rb if m not in ra]:
                own(self.change("media-type-added", f"request body {mt}", None, None, None, None, ob.id), "request")
            for mt in [m for m in ra if m not in rb]:
                own(self.change("media-type-removed", f"request body {mt}", None, None, None, oa.id, None),
                    "request")
            for mt in [m for m in ra if m in rb]:
                changes, children = self.matched(ga.nodes[ra[mt].target], gb.nodes[rb[mt].target], f"request {mt}")
                for c in changes:
                    own(c, "request")
                request_roots += children

        def table(graph, node):
            bodies, headers, statuses = {}, {}, set()
            for e in graph.outgoing(node.id, g.RETURNS):
                statuses.add(e.data.get("status"))
                if e.data.get("header"):
                    headers[(e.data.get("status"), e.data["header"])] = e
                else:
                    bodies[(e.data.get("status"), e.data.get("media_type"))] = e
            return bodies, headers, statuses

        ba, ha, sa = table(ga, oa)
        bb, hb, sb = table(gb, ob)
        sa |= set(oa.data.get("responses", ()))  # a response with no schema is still a response
        sb |= set(ob.data.get("responses", ()))
        for status in sorted(sb - sa):
            own(self.change("response-added", f"response {status}", None, None, None, None, ob.id), "response")
        for status in sorted(sa - sb):
            kind = "success-response-removed" if str(status).startswith("2") else "response-removed"
            own(self.change(kind, f"response {status}", None, None, None, oa.id, None), "response")
        for k in [k for k in bb if k not in ba and k[0] in sa]:
            own(self.change("media-type-added", f"response {k[0]} {k[1]}", None, None, None, None, bb[k].target),
                "response")
        for k in [k for k in ba if k not in bb and k[0] in sb]:
            own(self.change("media-type-removed", f"response {k[0]} {k[1]}", None, None, None, ba[k].target, None),
                "response")
        for k in [k for k in hb if k not in ha and k[0] in sa]:
            own(self.change("header-added", f"response {k[0]} header {k[1]}", None, None, None, None, hb[k].target),
                "response")
        for k in [k for k in ha if k not in hb and k[0] in sb]:
            own(self.change("header-removed", f"response {k[0]} header {k[1]}", None, None, None, ha[k].target,
                            None), "response")
        for table_a, table_b, label in ((ba, bb, "{0} {1}"), (ha, hb, "{0} header {1}")):
            for k in [k for k in table_a if k in table_b]:
                where = "response " + label.format(*k)
                changes, children = self.matched(ga.nodes[table_a[k].target], gb.nodes[table_b[k].target], where)
                for c in changes:
                    own(c, "response")
                response_roots += children
        return out + self.reach(request_roots, "request") + self.reach(response_roots, "response")

    def security(self, oa: Node, ob: Node) -> list[Change]:
        """How the ways to call an operation changed. Each way is a set of schemes used together; nothing
        declared, or `{}`, is anonymous access."""
        def ways(node):
            security = node.data.get("security")
            if not security:  # nothing declared, or `security: []`, which removes it
                return {frozenset(): {}}
            return {frozenset(w): w for w in security}

        def named(way):
            return " + ".join(sorted(way))

        wa, wb = ways(oa), ways(ob)
        out = []
        for k in [k for k in wa if k not in wb]:
            if k:
                out.append(self.change("security-removed", f"security {named(k)}", named(k), None, None, oa.id, ob.id))
            else:  # named after what is required now, which is what an answer would mention
                now = ", ".join(named(w) for w in wb if w)
                out.append(self.change("anonymous-access-removed", f"security {now}", "anonymous", now, None,
                                       oa.id, ob.id))
        # Anonymous access replaced: the change above already names every new way in.
        only_anonymous = set(wa) == {frozenset()} and frozenset() not in wb
        for k in [k for k in wb if k not in wa]:
            if k and not only_anonymous:
                out.append(self.change("security-added", f"security {named(k)}", None, named(k), None, oa.id, ob.id))
            elif not k:
                out.append(self.change("anonymous-access-added", "security", None, "anonymous", None, oa.id, ob.id))
        schemes_a, schemes_b = self.ga.security_schemes, self.gb.security_schemes
        changed = set()
        for k in [k for k in wa if k in wb]:
            for scheme in sorted(k):
                before, after = set(wa[k][scheme]), set(wb[k][scheme])
                if after - before:
                    out.append(self.change("scope-added", f"security {scheme}", ", ".join(sorted(before)) or None,
                                           ", ".join(sorted(after)), None, oa.id, ob.id))
                if before - after:
                    out.append(self.change("scope-removed", f"security {scheme}", ", ".join(sorted(before)),
                                           ", ".join(sorted(after)) or None, None, oa.id, ob.id))
                if scheme in changed:
                    continue
                changed.add(scheme)
                sa, sb = _how(schemes_a.get(scheme)), _how(schemes_b.get(scheme))
                if sa is not None and sb is not None and sa != sb:
                    out.append(self.change("security-scheme-changed", f"security {scheme}", sa, sb, None,
                                           oa.id, ob.id))
        return out

    def schema(self, sa: Node, sb: Node, side: str) -> list[Change]:
        return self.reach([("", self.pair(sa, sb))], side)

    def field(self, fa: Node, fb: Node, side: str) -> list[Change]:
        changes, children = self.typed(fa, fb, "", g.FIELD_TYPE)
        return [_rated(c, side, c.where) for c in changes] + self.reach(children, side)

    def shallow(self, sa: Node, sb: Node, side: str) -> list[Change]:
        """Only what changed in the schema itself, not below it: for listing which schemas changed."""
        key = self.pair(sa, sb)
        self.build()
        return [_rated(c, side, c.where) for c in self.pairs[key][0]]


def _how(scheme: dict | None) -> str | None:
    """What a client must do to use a security scheme, as text; None if the scheme is not defined."""
    if scheme is None:
        return None
    parts = [f"{k} {scheme[k]}" for k in ("type", "in", "name", "scheme", "openIdConnectUrl") if k in scheme]
    if scheme.get("flows"):
        parts.append("flows " + ", ".join(scheme["flows"]))
    return "; ".join(parts)


def _rated(c: Change, side: str, where: str) -> Change:
    return replace(c, side=side, level=_level(c.kind, side), where=where)


def _join(prefix: str, rest: str) -> str:
    if not prefix:
        return rest
    if not rest:
        return prefix
    if rest.startswith(("[]", "{}")):
        return prefix + rest
    return f"{prefix}: {rest}" if prefix.startswith(("response ", "request ", "option ", "parameter ")) and \
        ":" not in prefix else f"{prefix}.{rest}"


def _types(data: dict) -> set:
    """The stated types, without null (see _nullable)."""
    t = data.get("type")
    out = set(t) if isinstance(t, list) else ({t} if t else set())
    return out - {"null"}


def _nullable(data: dict) -> bool:
    t = data.get("type")
    return bool(data.get("nullable")) or (isinstance(t, list) and "null" in t) or t == "null"


def _allows_anything(graph: Graph, node: Node) -> bool:
    """`{}`: no type, no constraint, nothing below it. Not the same as a schema whose type comes from a parent."""
    constraining = {k for k in node.data if k not in ("title", "description", "named", "required", "deprecated",
                                                       "readOnly", "writeOnly", "default", "effective_required")}
    return not constraining and not node.data.get("required") and not any(graph.outgoing(node.id))


def _option_types(graph: Graph, node: Node) -> set:
    """The types a oneOf/anyOf allows, when every option states one."""
    out = set()
    for e in graph.outgoing(node.id, g.OPTION):
        t = _types(graph.nodes[e.target].data)
        if not t:
            return set()
        out |= t
    return out


def _structured(graph: Graph, node: Node) -> bool:
    return any(graph.outgoing(node.id, k) for k in (g.HAS_FIELD, g.ITEMS, g.OPTION, g.VALUES))


def _annotations(data: dict) -> dict:
    # Beside a $ref only these say anything about the field itself; its type comes from the schema.
    return {k: data[k] for k in ("description", "deprecated", "readOnly", "writeOnly", "default") if k in data}


def _fields(graph: Graph, schema: Node) -> dict:
    out = {}
    for e in graph.outgoing(schema.id, g.HAS_FIELD):
        name = graph.nodes[e.target].data["name"]
        out.setdefault(name, (graph.nodes[e.target], e))
    return out


def _edge_key(e) -> str:
    keyword = e.data.get("keyword", "")
    if "index" in e.data:
        return f"{keyword}/{e.data['index']}"
    if "pattern" in e.data:
        return f"{keyword}/{e.data['pattern']}"
    return keyword


def _wrapper_of(graph: Graph, node_id: str):
    """(property, schema name, schema id) when the schema is an object with one property whose type is a named
    schema, and nothing else that would matter: {circle: Circle}."""
    if graph.outgoing(node_id, g.OPTION) or graph.outgoing(node_id, g.CONDITIONAL):
        return None
    fields = graph.outgoing(node_id, g.HAS_FIELD)
    if len(fields) != 1:
        return None
    field = graph.nodes[fields[0].target]
    types = graph.outgoing(field.id, g.FIELD_TYPE)
    if len(types) != 1 or not graph.nodes[types[0].target].data.get("named"):
        return None
    target = graph.nodes[types[0].target]
    return field.data["name"], target.name, target.id


def _option_key(graph: Graph, e) -> str:
    target = graph.nodes[e.target]
    return target.name if target.data.get("named") else f"{e.data.get('keyword')}/{e.data.get('index')}"


def _side(spec, node_id: str) -> str:
    roles = {u["edge"] for u in spec.index.used_by(node_id)}
    request = bool(roles & {g.ACCEPTS, g.TAKES})
    response = g.RETURNS in roles
    return "both" if request and response else "request" if request else "response" if response else "none"


# ---- the public functions -----------------------------------------------------------------------

def _find(spec, kind: str, name: str, plain=True) -> Node | None:
    try:
        return _one(spec, kind, name, kind, plain=plain)
    except NotFound:
        return None


def compare(a, b, name: str) -> Comparison:
    """What changed in one schema, operation or field from spec `a` to spec `b`.

    `name` is a schema (`Pet`), an operation (`GET /pets/{id}`), or a field (`Pet.name`, following
    inheritance). Something found in only one spec is reported as added or removed."""
    c = _Comparer(a, b)
    parts = name.split(None, 1)
    if len(parts) == 2 and parts[0].upper() in ("GET", "PUT", "POST", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"):
        op_name = f"{parts[0].upper()} {parts[1]}"
        oa, ob = _find(a, g.OPERATION, op_name, False), _find(b, g.OPERATION, op_name, False)
        if not oa and not ob:
            raise NotFound("operation", name, a.index.names.suggest(g.OPERATION, op_name))
        if not oa or not ob:
            kind = "operation-added" if ob else \
                "deprecated-operation-removed" if oa.data.get("deprecated") else "operation-removed"
            changes = [_rated(c.change(kind, "", None, None, None, oa and oa.id, ob and ob.id), "request", "")]
        else:
            changes = c.operations(oa, ob)
        return Comparison("operation", op_name, a.label, b.label, oa and oa.id, ob and ob.id, "request", changes)

    sa, sb = _find(a, g.SCHEMA, name), _find(b, g.SCHEMA, name)
    if sa and sb and sa.name != sb.name:  # found by plain words as two different schemas
        sb = _find(b, g.SCHEMA, sa.name, False) or sb
    if sa or sb:
        if sa and not sb:
            sb = _find(b, g.SCHEMA, sa.name, False)
        if sb and not sa:
            sa = _find(a, g.SCHEMA, sb.name, False)
        side = _side(b, sb.id) if sb else _side(a, sa.id)
        if not sa or not sb:
            kind = "schema-added" if sb else "schema-removed"
            changes = [_rated(c.change(kind, "", None, None, None, sa and sa.id, sb and sb.id), side, "")]
            hint = _rename_hint(a, b, sa, sb)
            if hint:
                old, new, why = hint
                changes.append(replace(_rated(c.change("possibly-renamed", "", old.name, new.name, None, old.id,
                                                       new.id), side, ""), note=why))
        else:
            changes = c.schema(sa, sb, side)
        return Comparison("schema", (sb or sa).name, a.label, b.label, sa and sa.id, sb and sb.id, side, changes)

    if "." in name:
        fa, fb = _field(a, name), _field(b, name)
        if fa or fb:
            side = _side(b, fb.id) if fb else _side(a, fa.id)
            if not fa or not fb:
                kind = "field-added" if fb else "field-removed"
                node = fb or fa
                changes = [_rated(c.change(kind, "", None if fb else describe(a.graph, fa),
                                           describe(b.graph, fb) if fb else None, None, fa and fa.id, fb and fb.id),
                                  side, "")]
                del node
            else:
                changes = c.field(fa, fb, side)
            return Comparison("field", name, a.label, b.label, fa and fa.id, fb and fb.id, side, changes)
    raise NotFound("schema, operation or field", name, a.index.names.suggest(g.SCHEMA, name))


def _field(spec, name: str) -> Node | None:
    schema_name, field_name = name.rsplit(".", 1)
    schema = _find(spec, g.SCHEMA, schema_name)
    if schema is None:
        return None
    ids = spec.graph.resolve(f"{spec.label}:{g.FIELD}/{schema.name}.{field_name}")
    return spec.graph.nodes[ids[0]] if ids else None


@dataclass
class SpecDiff(Result):
    a_label: str
    b_label: str
    operations_added: list[str]
    operations_removed: list[str]
    operations_changed: dict[str, list[Change]]
    removed_after_deprecation: list[str]  # removed operations that were deprecated first: not breaking
    schemas_added: list[str]
    schemas_removed: list[str]
    schemas_changed: dict[str, list[Change]]
    possible_renames: dict[str, dict] = field(default_factory=dict)  # removed -> {"to": added, "why": ...}
    security_schemes_added: list[str] = field(default_factory=list)  # how they are used is in operations_changed
    security_schemes_removed: list[str] = field(default_factory=list)
    # A field gone and one added in the same schema, the same name but for case or separators (callSign ->
    # callsign): {"schema", "from", "to", "why"}
    possible_field_renames: list[dict] = field(default_factory=list)
    # A parameter gone and one added in the same place of the same operations, the only such pair in each:
    # {"in", "from", "to", "operations", "why"}
    possible_parameter_renames: list[dict] = field(default_factory=list)

    def breaking(self) -> list[tuple[str, Change]]:
        """Every breaking change, by the operation it breaks (removed operations included)."""
        out = [(op, Change("operation-removed", "", None, None, BREAKING, "request")) for op in self.operations_removed
               if op not in self.removed_after_deprecation]
        for op, changes in self.operations_changed.items():
            out += [(op, c) for c in changes if c.level == BREAKING]
        return out

    def to_text(self, full: bool = False) -> str:
        """The counts, likely renames, every breaking change by operation, what was added and removed, and how many
        other changes there are. `full=True` also lists those other changes (warning and info), by schema and by
        operation; JSON always has every change."""
        lines = [f"diff {self.a_label} -> {self.b_label}"]
        lines.append(f"operations: {len(self.operations_added)} added, {len(self.operations_removed)} removed, "
                     f"{len(self.operations_changed)} changed")
        lines.append(f"schemas: {len(self.schemas_added)} added, {len(self.schemas_removed)} removed, "
                     f"{len(self.schemas_changed)} changed")
        for old, hint in self.possible_renames.items():
            lines.append(f"possibly renamed: {old} -> {hint['to']} ({hint['why']})")
        for r in self.possible_field_renames:
            lines.append(f"possibly renamed: field {r['schema']}.{r['from']} -> {r['to']} ({r['why']})")
        for r in self.possible_parameter_renames:
            n = len(r["operations"])
            lines.append(f"possibly renamed: {r['in']} parameter {r['from']} -> {r['to']} in {n} operation"
                         f"{'s' * (n != 1)} ({r['why']})")
        breaking = self.breaking()
        ops = sorted({op for op, _ in breaking})
        # Each distinct change once, where it is first met, with the other operations it breaks: a change to a
        # shared schema is one line, not one per operation.
        groups: dict[tuple, list[tuple[str, Change]]] = {}
        for op, c in sorted(breaking, key=lambda oc: oc[0]):
            groups.setdefault((c.kind, c.before, c.after, c.a, c.b), []).append((op, c))
        lines.append(f"breaking: {len(breaking)} change{'s' * (len(breaking) != 1)}"
                     + (f" ({len(groups)} distinct)" if len(groups) != len(breaking) else "")
                     + f" in {len(ops)} operation{'s' * (len(ops) != 1)}" + (":" if breaking else ""))
        shown = list(groups.values()) if full else list(groups.values())[:50]
        for found in shown:
            op, c = found[0]
            first = replace(c, where=f"{op} {c.where}".strip())
            lines.append(f"  {first.line()}")
            others = list(dict.fromkeys(o for o, _ in found[1:] if o != op))
            if others:
                listed = others if full else others[:10]
                lines.append(f"    also in {len(others)} operation{'s' * (len(others) != 1)}: " + ", ".join(listed)
                             + (f" … and {len(others) - len(listed)} more" if len(listed) < len(others) else ""))
        if len(shown) < len(groups):
            lines.append(f"  … and {len(groups) - len(shown)} more distinct changes")
        for title, items in (("operations added", self.operations_added), ("operations removed", self.operations_removed),
                             ("schemas added", self.schemas_added), ("schemas removed", self.schemas_removed),
                             ("security schemes added", self.security_schemes_added),
                             ("security schemes removed", self.security_schemes_removed)):
            if items:
                shown = ", ".join(items[:20]) + (f" … and {len(items) - 20} more" if len(items) > 20 else "")
                lines.append(f"{title}: {shown}")
        others = [(where, c) for where, cs in list(self.schemas_changed.items()) + list(self.operations_changed.items())
                  for c in cs if c.level != BREAKING]
        if others:
            n = {level: sum(c.level == level for _, c in others) for level in (WARNING, INFO)}
            lines.append(f"other changes: {n[WARNING]} warning, {n[INFO]} info"
                         + (":" if full else " (to_text(full=True) or JSON lists them)"))
        if full:
            for title, changed in (("schema", self.schemas_changed), ("operation", self.operations_changed)):
                for name, cs in changed.items():
                    grouped = _grouped([c for c in cs if c.level != BREAKING])
                    if grouped:
                        lines.append(f"  {title} {name}")
                        lines += [f"    {line}" for line in grouped]
        return "\n".join(lines)


def diff(a, b) -> SpecDiff:
    """Everything that changed from spec `a` to spec `b`: operations, then named schemas."""
    c = _Comparer(a, b)
    ops_a = {n.name: n for n in a.graph.of_kind(g.OPERATION)}
    ops_b = {n.name: n for n in b.graph.of_kind(g.OPERATION)}
    changed = {}
    for name in sorted(set(ops_a) & set(ops_b)):
        changes = c.operations(ops_a[name], ops_b[name])
        if changes:
            changed[name] = changes
    sch_a = {n.name: n for n in a.graph.of_kind(g.SCHEMA) if n.data.get("named")}
    sch_b = {n.name: n for n in b.graph.of_kind(g.SCHEMA) if n.data.get("named")}
    schemas_changed = {}
    for name in sorted(set(sch_a) & set(sch_b)):
        changes = c.shallow(sch_a[name], sch_b[name], _side(b, sch_b[name].id))
        if changes:
            schemas_changed[name] = changes
    removed = sorted(set(ops_a) - set(ops_b))
    schemas_removed, schemas_added = sorted(set(sch_a) - set(sch_b)), sorted(set(sch_b) - set(sch_a))
    renames = _renames(a.graph, b.graph, [sch_a[n] for n in schemas_removed], [sch_b[n] for n in schemas_added])
    field_renames = []
    for name, changes in schemas_changed.items():
        removed_ = [c for c in changes if c.kind in ("field-removed", "required-field-removed") and "." not in c.where]
        added_ = [c for c in changes if c.kind in ("field-added", "required-field-added") and "." not in c.where]
        gone = {key(c.where): c for c in removed_}
        paired = set()
        for c in added_:
            if key(c.where) in gone and gone[key(c.where)].where != c.where:
                field_renames.append({"schema": name, "from": gone[key(c.where)].where, "to": c.where,
                                      "why": "the same name but for case or separators"})
                paired |= {id(c), id(gone[key(c.where)])}
        removed_ = [c for c in removed_ if id(c) not in paired]
        added_ = [c for c in added_ if id(c) not in paired]
        # The only field gone and the only one added, of the same type: eta -> estimatedArrival.
        if len(removed_) == 1 and len(added_) == 1 and removed_[0].before == added_[0].after:
            field_renames.append({"schema": name, "from": removed_[0].where, "to": added_[0].where,
                                  "why": f"the only field removed and the only one added, both {added_[0].after}"})
    result = SpecDiff(a.label, b.label, sorted(set(ops_b) - set(ops_a)), removed, changed,
                    [op for op in removed if ops_a[op].data.get("deprecated")],
                    schemas_added, schemas_removed, schemas_changed,
                    {old: {"to": new.name, "why": why} for old, (new, why) in renames.items()},
                    sorted(set(b.graph.security_schemes) - set(a.graph.security_schemes)),
                    sorted(set(a.graph.security_schemes) - set(b.graph.security_schemes)), field_renames,
                    _parameter_renames(changed))
    return result


def _parameter_renames(changed: dict[str, list[Change]]) -> list[dict]:
    """A parameter gone and one added in the same place (query, header, ...) of an operation, the only such
    pair there, listed once with every operation where it is found: page -> cursor. A hint: the types may differ."""
    pairs: dict[tuple, list[str]] = {}
    for op, changes in changed.items():
        by_place: dict[str, tuple[list, list]] = {}
        for c in changes:
            if c.kind in ("parameter-removed", "parameter-added", "required-parameter-added") and \
                    c.where.startswith("parameter "):
                _, place, name = c.where.split(" ", 2)
                by_place.setdefault(place, ([], []))[c.kind != "parameter-removed"].append((name, c))
        for place, (gone, new) in by_place.items():
            if len(gone) == 1 and len(new) == 1:
                (old_name, old), (new_name, added) = gone[0], new[0]
                pairs.setdefault((place, old_name, new_name, old.before, added.after), []).append(op)
    out = []
    for (place, old_name, new_name, before, after), ops in pairs.items():
        types = f"both {after}" if before == after else f"{before} -> {after}"
        out.append({"in": place, "from": old_name, "to": new_name, "operations": ops,
                    "why": f"the only {place} parameter removed and the only one added, {types}"})
    return out


def _rename_hint(a, b, sa: Node | None, sb: Node | None):
    """For a schema in only one of the specs: (old, new, why) if one on the other side looks like it renamed."""
    named_a = {n.name for n in a.graph.of_kind(g.SCHEMA) if n.data.get("named")}
    named_b = {n.name for n in b.graph.of_kind(g.SCHEMA) if n.data.get("named")}
    if sa:
        added = [n for n in b.graph.of_kind(g.SCHEMA) if n.data.get("named") and n.name not in named_a]
        found = _renames(a.graph, b.graph, [sa], added)
        return (sa, found[sa.name][0], found[sa.name][1]) if sa.name in found else None
    gone = [n for n in a.graph.of_kind(g.SCHEMA) if n.data.get("named") and n.name not in named_b]
    for old, (new, why) in _renames(a.graph, b.graph, gone, [sb]).items():
        return a.graph.nodes[f"{a.label}:{g.SCHEMA}/{old}"], new, why
    return None


def _contents(graph: Graph, node: Node) -> tuple[str, set]:
    """What a schema holds, to recognise it under another name: its fields, else its enum values."""
    fields = {graph.nodes[e.target].data["name"] for e in graph.outgoing(node.id, g.HAS_FIELD)}
    if fields:
        return "fields", fields
    if isinstance(node.data.get("enum"), list):
        return "enum values", {repr(v) for v in node.data["enum"]}
    return "", set()


def _renames(ga: Graph, gb: Graph, removed: list[Node], added: list[Node]) -> dict[str, tuple[Node, str]]:
    """Removed schemas that look renamed: an added schema holding mostly the same fields (or enum values).
    A hint, matched one to one, best first; at least 3 shared and two thirds of either side."""
    candidates = []
    for r in removed:
        what, ra = _contents(ga, r)
        if len(ra) < 3:
            continue
        for n in added:
            what_b, nb = _contents(gb, n)
            shared = ra & nb
            if what_b != what or len(shared) < 3:
                continue
            score = len(shared) / len(ra | nb)
            if score >= 2 / 3:
                why = f"{len(shared)} of {len(ra)} {what} kept" + (f", {len(nb - ra)} new" if nb - ra else "")
                candidates.append((score, r.name, n, why))
    out, taken = {}, set()
    for score, old, new, why in sorted(candidates, key=lambda c: (-c[0], c[1], c[2].name)):
        if old not in out and new.name not in taken:
            out[old] = (new, why)
            taken.add(new.name)
    return out
