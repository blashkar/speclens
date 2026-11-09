"""Check speclens against an independent, deliberately naive reading of a spec.

    uv run python tools/crosscheck.py path/to/openapi.yaml [more specs...]

Shares no code with speclens. For each spec it recomputes, by plain recursion over the YAML:
  1. the operations under `paths`;
  2. every schema node's effective fields: its own properties, inline allOf branches, allOf `$ref` ancestors,
     and in 3.1 a `$ref` beside other keywords;
  3. every response's target schema (operation, status, media type -> where the schema is written);
  4. for every schema, the operations that reach it (by walking forward from each operation through every
     subschema keyword), against speclens's used-by;
  5. that every named schema and every operation is found by its own name, as itself.
It prints any difference. Exit code 1 if there is one.

Use it on any spec, including ones that must not be committed: it reads them in place and writes nothing.
"""

import os
import re
import sys
from urllib.parse import unquote, urlsplit

import yaml

from speclens import load_spec

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
ANNOTATIONS = {"$ref", "description", "title", "summary", "default", "deprecated", "readOnly", "writeOnly",
               "examples", "example", "$comment"}
# Every value as text: the checks here are about structure (keys and nesting), not values.
_Loader = getattr(yaml, "CBaseLoader", yaml.BaseLoader)
_cache = {}


def _doc(path):
    path = os.path.realpath(path)
    if path not in _cache:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        try:
            _cache[path] = yaml.load(text, Loader=_Loader)
        except yaml.YAMLError:
            # What YAML does not allow but specs contain: tabs on blank lines, stray control characters.
            text = "\n".join("" if line.strip(" \t") == "" else line for line in text.split("\n"))
            text = re.sub("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f\ufffe\uffff]", "?", text)
            _cache[path] = yaml.load(text, Loader=_Loader)
    return _cache[path]


def _get(path, ptr):
    value = _doc(path)
    for token in ptr[1:].split("/") if ptr else []:
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def _esc(token):
    return str(token).replace("~", "~0").replace("/", "~1")


def _follow(path, ptr, value, schema=False, v30=False):
    """Follow `$ref` chains to (path, ptr, value). For a 3.1 schema, stop at a `$ref` with real keywords beside
    it; in 3.0 those keywords are ignored."""
    seen = set()
    while isinstance(value, dict) and isinstance(value.get("$ref"), str):
        if schema and not v30 and set(value) - ANNOTATIONS:
            break
        if (path, ptr) in seen:
            return None
        seen.add((path, ptr))
        ref = urlsplit(value["$ref"])
        if ref.path:
            path = os.path.realpath(os.path.join(os.path.dirname(path), unquote(ref.path)))
        ptr = unquote(ref.fragment)
        value = _get(path, ptr)
    return path, ptr, value


def _fields(path, ptr, value, v30, stack=()):
    if (path, ptr) in stack or not isinstance(value, dict):
        return set()
    stack += ((path, ptr),)
    out = set()
    if "$ref" in value:
        target = _follow(path, ptr, {"$ref": value["$ref"]})
        out = _fields(*target, v30, stack) if target else set()
        if v30:
            return out  # 3.0 ignores everything beside a $ref
    out |= set((value.get("properties") or {}).keys())
    for i, branch in enumerate(value.get("allOf") or []):
        out |= _fields(path, f"{ptr}/allOf/{i}", branch, v30, stack)
    return out


SUBSCHEMAS = ("items", "not", "if", "then", "else", "contains", "additionalProperties", "unevaluatedProperties",
              "unevaluatedItems", "propertyNames")
SCHEMA_LISTS = ("allOf", "oneOf", "anyOf", "prefixItems")
SCHEMA_MAPS = ("properties", "patternProperties", "dependentSchemas")


def _reach(path, ptr, value, v30, seen):
    """Every schema location reachable from a schema, following $ref."""
    stack = [(path, ptr, value)]
    while stack:
        path, ptr, value = stack.pop()
        if not isinstance(value, dict):
            continue
        if "$ref" in value and isinstance(value["$ref"], str):
            target = _follow(path, ptr, {"$ref": value["$ref"]})
            pure = v30 or not (set(value) - ANNOTATIONS)
            if target:
                stack.append(target)
            if pure:
                continue
        if (path, ptr) in seen:
            continue
        seen.add((path, ptr))
        for k in SUBSCHEMAS:
            if isinstance(value.get(k), dict):
                stack.append((path, f"{ptr}/{k}", value[k]))
        for k in SCHEMA_LISTS:
            for i, sub in enumerate(value.get(k) or []):
                stack.append((path, f"{ptr}/{k}/{i}", sub))
        for k in SCHEMA_MAPS:
            for name, sub in (value.get(k) or {}).items():
                stack.append((path, f"{ptr}/{k}/{_esc(name)}", sub))
    return seen


def _operation_schemas(opath, optr, op, shared):
    """(path, ptr, schema) for every schema an operation names directly: parameters, request, responses."""
    out = []
    params = list(shared) + [(opath, f"{optr}/parameters/{i}", p) for i, p in enumerate(op.get("parameters") or [])]
    for ppath, pptr, param in params:
        found = _follow(ppath, pptr, param)
        if found and isinstance(found[2], dict):
            fpath, fptr, param = found
            if "schema" in param:
                out.append((fpath, f"{fptr}/schema", param["schema"]))
            for mt, media in (param.get("content") or {}).items():
                if isinstance(media, dict) and "schema" in media:
                    out.append((fpath, f"{fptr}/content/{_esc(mt)}/schema", media["schema"]))
    body = op.get("requestBody")
    if body is not None:
        found = _follow(opath, f"{optr}/requestBody", body)
        if found and isinstance(found[2], dict):
            bpath, bptr, body = found
            for mt, media in (body.get("content") or {}).items():
                if isinstance(media, dict) and "schema" in media:
                    out.append((bpath, f"{bptr}/content/{_esc(mt)}/schema", media["schema"]))
    for status, response in (op.get("responses") or {}).items():
        found = _follow(opath, f"{optr}/responses/{_esc(status)}", response)
        if not found or not isinstance(found[2], dict):
            continue
        rpath, rptr, response = found
        for mt, media in (response.get("content") or {}).items():
            if isinstance(media, dict) and "schema" in media:
                out.append((rpath, f"{rptr}/content/{_esc(mt)}/schema", media["schema"]))
        for name, header in (response.get("headers") or {}).items():
            found = _follow(rpath, f"{rptr}/headers/{_esc(name)}", header)
            if found and isinstance(found[2], dict) and "schema" in found[2]:
                out.append((found[0], f"{found[1]}/schema", found[2]["schema"]))
    return out


def check(root) -> list[str]:
    root = os.path.realpath(root)
    base = os.path.dirname(root)
    spec = load_spec(root, "x")
    g = spec.graph
    v30 = spec.version == "3.0"
    problems = []

    def location(path, ptr):
        return os.path.relpath(path, base).replace(os.sep, "/") + "#" + ptr

    paths = []
    for p, item in (_doc(root).get("paths") or {}).items():
        found = _follow(root, "/paths/" + _esc(p), item)
        if found:
            paths.append((p, *found))

    want_ops = {f"{m.upper()} {p}" for p, _, _, item in paths for m in METHODS if isinstance(item.get(m), dict)}
    got_ops = {n.name for n in g.of_kind("operation") if not n.name.split(" ", 1)[1].startswith("webhook:")}
    if want_ops != got_ops:
        problems.append(f"operations: missing {sorted(want_ops - got_ops)[:5]}, extra {sorted(got_ops - want_ops)[:5]}")

    schemas = 0
    for node in g.of_kind("schema"):
        file, ptr = node.location.split("#", 1)
        path = os.path.realpath(os.path.join(base, file))
        want = _fields(path, ptr, _get(path, ptr), v30)
        got = {g.nodes[e.target].data["name"] for e in g.outgoing(node.id, "has_field")}
        schemas += 1
        if want != got:
            problems.append(f"fields of {node.id}: missing {sorted(want - got)[:5]}, extra {sorted(got - want)[:5]}")

    responses = 0
    for p, ipath, iptr, item in paths:
        for m in METHODS:
            if not isinstance(item.get(m), dict):
                continue
            opath, optr, op = _follow(ipath, f"{iptr}/{m}", item[m])
            got = {(e.data["status"], e.data["media_type"]): g.nodes[e.target].location
                   for e in g.outgoing(f"x:operation/{m.upper()} {p}", "returns") if "media_type" in e.data}
            for status, response in (op.get("responses") or {}).items():
                found = _follow(opath, f"{optr}/responses/{_esc(status)}", response)
                if not found:
                    continue
                rpath, rptr, response = found
                for media_type, media in ((response or {}).get("content") or {}).items():
                    if not isinstance(media, dict) or not isinstance(media.get("schema"), dict):
                        continue
                    found = _follow(rpath, f"{rptr}/content/{_esc(media_type)}/schema", media["schema"], schema=True,
                                    v30=v30)
                    if not found:
                        continue
                    responses += 1
                    want = location(found[0], found[1])
                    if got.get((str(status), media_type)) != want:
                        problems.append(f"response {m.upper()} {p} {status} {media_type}: "
                                        f"want {want}, got {got.get((str(status), media_type))}")

    # 4. used by: walk forward from each operation, compare per schema node
    reached_by: dict[str, set] = {}
    for p, ipath, iptr, item in paths:
        shared = [(ipath, f"{iptr}/parameters/{i}", prm) for i, prm in enumerate(item.get("parameters") or [])]
        for m in METHODS:
            if not isinstance(item.get(m), dict):
                continue
            opath, optr, op = _follow(ipath, f"{iptr}/{m}", item[m])
            seen = set()
            for spath, sptr, schema in _operation_schemas(opath, optr, op, shared):
                _reach(spath, sptr, schema, v30, seen)
            for spath, sptr in seen:
                reached_by.setdefault(location(spath, sptr), set()).add(f"x:operation/{m.upper()} {p}")
    used = 0
    for node in g.of_kind("schema"):
        want = reached_by.get(node.location, set())
        got = {u["operation"] for u in spec.index.used_by(node.id)
               if not u["operation"].split(" ", 1)[1].startswith("webhook:")}
        used += 1
        if want != got:
            problems.append(f"used by of {node.id}: missing {sorted(want - got)[:3]}, extra {sorted(got - want)[:3]}")

    # 5. every named schema and operation is found by its own name
    for node in g.of_kind("schema"):
        if node.data.get("named"):
            try:
                found = spec.get_schema(node.name).id
            except LookupError as e:
                found = f"error: {e}"
            if found != node.id:
                problems.append(f"get_schema({node.name!r}) gave {found}, not {node.id}")
    for node in g.of_kind("operation"):
        found = spec.get_operation(node.name).id
        if found != node.id:
            problems.append(f"get_operation({node.name!r}) gave {found}, not {node.id}")

    named_by_location = sum("#/" in n.name for n in g.nodes.values())
    print(f"{os.path.basename(root)}: {len(want_ops)} operations, {schemas} schemas, {responses} responses, "
          f"{used} used-by sets; "
          f"{len(problems)} differences; {len(spec.issues)} issues; {named_by_location} ids named by location")
    for problem in problems[:10]:
        print("   ", problem)
    return problems


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 2
    return 1 if any([check(path) for path in argv]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
