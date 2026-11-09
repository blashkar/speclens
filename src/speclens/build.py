"""Turn the files of one spec into a Graph: resolve every `$ref`, walk the OpenAPI structure, name the nodes.

Naming happens last and depends only on where a node was written, never on the order files were read, so
ids are the same on every run. See `_names`.
"""

from collections import deque
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.request import url2pathname
import os

from . import graph as g
from . import pointer
from .graph import Edge, Graph, Node
from .issues import ERROR, WARNING, Issue
from .loader import Document, load

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
KNOWN_DIALECTS = {
    "https://spec.openapis.org/oas/3.1/dialect/base",
    "https://json-schema.org/draft/2020-12/schema",
}
# Keywords that may sit next to a `$ref` without making a new schema: they describe, they don't constrain.
ANNOTATIONS = {"title", "description", "summary", "default", "deprecated", "readOnly", "writeOnly",
               "examples", "example", "$comment"}
# Subschema keywords that make a schema more than a single typed value.
STRUCTURAL = {"properties", "allOf", "oneOf", "anyOf", "not", "if", "then", "else", "items", "prefixItems",
              "contains", "patternProperties", "dependentSchemas", "unevaluatedProperties",
              "unevaluatedItems", "propertyNames", "additionalProperties"}
# Keywords whose next token is a name or an index, kept together in a node's name: `#oneOf/0`.
PAIRED = {"allOf", "anyOf", "oneOf", "prefixItems", "patternProperties", "dependentSchemas", "$defs",
          "definitions"}
# Copied from a schema or property into its node, as written.
SCHEMA_DATA = ("title", "description", "type", "format", "enum", "const", "default", "nullable", "deprecated",
               "readOnly", "writeOnly", "pattern", "minimum", "maximum", "exclusiveMinimum",
               "exclusiveMaximum", "minLength", "maxLength", "minItems", "maxItems", "uniqueItems",
               "dependentRequired", "multipleOf", "minProperties", "maxProperties", "minContains", "maxContains")
# Values that are data, not spec structure: a `$ref` inside them is not a reference.
LITERALS = {"example", "default", "enum", "const"}
# Maps whose keys are names chosen by the spec's author, so a key there is never a keyword.
NAME_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas", "schemas",
             "responses", "parameters", "requestBodies", "headers", "securitySchemes", "links", "callbacks",
             "pathItems", "paths", "webhooks", "content", "encoding", "variables"}


class _Builder:
    def __init__(self, roots: list[Path], label: str, allow: list | None = None):
        self.label = label
        self.allow = [Path(a).resolve() for a in allow] if allow is not None else None
        self.roots = [r.resolve() for r in roots]
        # File names are relative to the folder holding every root, so they stay as written.
        self.root_dir = Path(os.path.commonpath([r.parent for r in self.roots])) if self.roots else Path.cwd()
        self.docs: dict[Path, Document] = {}
        self.issues: list[Issue] = []
        self.version = None
        self.pending: dict[tuple, dict] = {}  # (kind, ident) -> {"doc", "ptr", "data", "name"?}
        self.edges: list[tuple[str, tuple, tuple, dict]] = []
        self.queue = deque()
        self.merged: set[tuple[str, str]] = set()  # inline allOf branches, folded into the schema holding them
        self.fold = True  # whether names leave those branches out; see `names`
        self.named: set[tuple] = set()
        self.required: dict[tuple, set] = {}  # schema key -> names its own `required` lists
        self.resolved: dict[tuple[str, str, str], tuple | None] = {}  # (file, pointer, keyword) -> target
        self.walked_components: set[Path] = set()
        self.warned: set[tuple] = set()
        self.merged_into: dict[tuple, tuple] = {}  # a folded allOf branch -> (the schema holding it, index)
        self.security_schemes: dict[str, dict] = {}  # the root documents' securitySchemes, by name
        self.used_schemes: set[str] = set()  # the schemes some operation's requirement names
        self.path_items: dict[tuple[str, str], set[str]] = {}  # where a path item was written -> its paths
        self.op_locations: dict[tuple[str, str], set[str]] = {}  # an operation kept elsewhere -> its names
        # JSON Schema 2020-12 identifiers (3.1 only): a base URI -> the schema resource it names, an anchor in a
        # resource -> where it is, a dynamic anchor's name -> every schema that has it.
        self.ids: dict[str, tuple[Document, str]] = {}
        self.bases: dict[tuple[str, str], str] = {}  # (file, pointer) of a schema with $id -> its base URI
        self.anchors: dict[tuple[str, str], tuple[Document, str]] = {}
        self.dynamic: dict[str, list[tuple[Document, str]]] = {}
        self.indexed: set[Path] = set()
        self.links: dict[str, set[str]] = {}  # a schema resource -> the resources its references reach
        # A component that is only a $ref: its node -> the schema it names. When both would get the same name
        # (`Pet: {$ref: './schemas.yaml#/Pet'}`), they are one schema, and the alias is dropped.
        self.alias_of: dict[tuple, tuple] = {}
        self.dropped_aliases: dict[tuple, tuple] = {}  # an alias dropped for its target -> that target
        # A schema with a discriminator -> (its property, value -> the schema it names), oneOf or allOf alike
        self.mappings: dict[tuple, tuple[str, dict[str, tuple]]] = {}
        # Where a component kept in another file is (`limit: {$ref: './parameters.yaml#/limit'}`) -> what it is
        # ("parameters/limit"), so a file of parameters or headers is not read as a file of schemas.
        self.component_at: dict[tuple[str, str], str] = {}

    # ---- files and references ----------------------------------------------------------------

    def document(self, path: Path) -> Document:
        path = path.resolve()
        if path not in self.docs:
            name = Path(os.path.relpath(path, self.root_dir)).as_posix()
            if self.allow is not None and not any(path == a or path.is_relative_to(a) for a in self.allow):
                doc = Document(path=path, name=name, text="")
                doc.issues.append(Issue(ERROR, "outside-allowed", "not under a folder that may be read; not read",
                                        name))
            elif not path.is_file():
                doc = Document(path=path, name=name, text="")
                doc.issues.append(Issue(ERROR, "missing-file", "file not found", name))
            else:
                doc = load(path, name)
            self.docs[path] = doc
            self.issues.extend(doc.issues)
        return self.docs[path]

    def line(self, doc: Document, ptr: str) -> int | None:
        return doc.line_of(ptr)

    def resolve_ref(self, doc: Document, ptr: str, keyword: str = "$ref"):
        """The (document, pointer) the `$ref` (or `$dynamicRef`) at `ptr` points to, or None after reporting why
        not."""
        cache_key = (doc.name, ptr, keyword)
        if cache_key in self.resolved:
            return self.resolved[cache_key]
        ref_ptr = pointer.join(ptr, keyword)
        ref = pointer.lookup(doc.data, ref_ptr)
        result, problem = self.locate(doc, ptr, ref)
        if problem:
            self.issues.append(Issue(ERROR, problem[0], f"{keyword} {ref!r}: {problem[1]}", doc.name,
                                     self.line(doc, ref_ptr)))
        self.resolved[cache_key] = result
        return result

    def locate(self, doc: Document, ptr: str, ref: str, base: str | None = None):
        """((document, pointer), None) for a reference written at `ptr`, or (None, (code, why)).

        In 3.1 a reference is a URI read against the base URI where it is written: the file, or the nearest
        `$id` around it. It names a schema resource (a file, or a schema with `$id`), then a JSON Pointer
        inside that resource or an anchor (`$anchor`, `$dynamicAnchor`) in it. `base` overrides the base."""
        self.index(doc)
        uri, fragment = urldefrag(_join(base or self.base_at(doc, ptr), ref))
        resource = self.ids.get(uri)
        if resource is None:
            parts = urlsplit(uri)
            if parts.scheme == "file":
                target = self.document(Path(url2pathname(parts.path)))
                self.index(target)
                resource = (target, "")
            else:
                written = urlsplit(ref)
                remote = f"no loaded schema has $id {uri!r}, and remote references are not supported"
                if written.scheme or not written.path:
                    return None, ("unsupported-ref", remote)
                # A relative path under an $id that names no loaded schema: a file there, if there is one.
                p = Path(url2pathname(written.path))
                p = p if p.is_absolute() else doc.path.parent / p
                if not p.is_file():
                    return None, ("unsupported-ref", f"no file {written.path!r} next to {doc.name}, and {remote}")
                target = self.document(p)
                self.index(target)
                resource = (target, "")
        target, base_ptr = resource
        resource_uri = self.bases.get((target.name, base_ptr), target.path.as_uri())
        self.links.setdefault(self.base_at(doc, ptr), set()).add(resource_uri)
        fragment = pointer.from_fragment(fragment)
        if target.data is None:
            return None, ("unresolved-ref", f"{target.name} is empty" if not target.issues
                          else f"{target.name} could not be read")
        if fragment and not fragment.startswith("/"):
            found = self.anchors.get((resource_uri, fragment))
            if found is None:
                return None, ("unresolved-ref", f"no anchor {fragment!r} in {target.name}")
            return found, None
        try:
            pointer.lookup(target.data, base_ptr + fragment)
        except KeyError as e:
            return None, ("unresolved-ref", f"{e.args[0]!r} not found in {target.name}")
        return (target, base_ptr + fragment), None

    def index(self, doc: Document):
        """Record the `$id`, `$anchor` and `$dynamicAnchor` of every schema in a file (3.1 only)."""
        if doc.path in self.indexed:
            return
        self.indexed.add(doc.path)
        self.ids.setdefault(doc.path.as_uri(), (doc, ""))
        if self.version == "3.0" or doc.data is None:
            return

        def walk(value, ptr: str, base: str, names: bool):
            if isinstance(value, dict):
                if not names:
                    if isinstance(value.get("$id"), str):
                        base = urldefrag(_join(base, value["$id"]))[0]
                        if self.ids.get(base, (doc, ptr)) != (doc, ptr):
                            other = self.ids[base]
                            self.issues.append(Issue(ERROR, "duplicate-id", f"$id {base!r} is also used at "
                                                     f"{other[0].name}#{other[1]}; this one is ignored", doc.name,
                                                     self.line(doc, pointer.join(ptr, "$id"))))
                        else:
                            self.ids[base] = (doc, ptr)
                            self.bases[(doc.name, ptr)] = base
                    for keyword in ("$anchor", "$dynamicAnchor"):
                        if isinstance(value.get(keyword), str):
                            self.anchors.setdefault((base, value[keyword]), (doc, ptr))
                    if isinstance(value.get("$dynamicAnchor"), str):
                        self.dynamic.setdefault(value["$dynamicAnchor"], []).append((doc, ptr, base))
                for k, v in value.items():
                    if not names and (k in LITERALS or k == "examples" or k.startswith("x-")):
                        continue
                    walk(v, pointer.join(ptr, k), base, not names and k in NAME_MAPS)
            elif isinstance(value, list):
                for i, v in enumerate(value):
                    walk(v, pointer.join(ptr, i), base, False)

        walk(doc.data, "", doc.path.as_uri(), False)

    def base_at(self, doc: Document, ptr: str) -> str:
        """The base URI a reference written at `ptr` is read against: the nearest `$id` around it, or the file."""
        base, here = self.bases.get((doc.name, ""), doc.path.as_uri()), ""
        for token in pointer.split(ptr):
            here = pointer.join(here, token)
            base = self.bases.get((doc.name, here), base)
        return base

    def deref(self, value, doc: Document, ptr: str, is_schema=False):
        """Follow `$ref` until reaching the object itself: (value, doc, ptr), or None if a reference is broken.

        For a schema, only a reference with nothing beside it but annotations is followed; anything else is a
        schema of its own (3.1). In 3.0 everything beside a `$ref` is ignored."""
        seen = set()
        while isinstance(value, dict) and isinstance(value.get("$ref"), str):
            if is_schema and not self.is_pure_ref(value, doc, ptr):
                break
            if (doc.name, ptr) in seen:
                self.issues.append(Issue(ERROR, "circular-ref", "these references only point to each other",
                                         doc.name, self.line(doc, ptr)))
                return None
            seen.add((doc.name, ptr))
            target = self.resolve_ref(doc, ptr)
            if target is None:
                return None
            doc, ptr = target
            value = pointer.lookup(doc.data, ptr)
        return value, doc, ptr

    def is_pure_ref(self, schema: dict, doc: Document, ptr: str) -> bool:
        extra = set(schema) - {"$ref"} - ANNOTATIONS
        if self.version == "3.0":
            if extra and (doc.name, ptr) not in self.warned:
                self.warned.add((doc.name, ptr))
                self.issues.append(Issue(WARNING, "ignored-beside-ref",
                                         f"OpenAPI 3.0 ignores {', '.join(sorted(extra))} next to $ref",
                                         doc.name, self.line(doc, ptr)))
            return True
        return not extra

    def ref_at(self, value, doc: Document, ptr: str) -> str | None:
        if isinstance(value, dict) and "$ref" in value:
            return f"{doc.name}:{self.line(doc, pointer.join(ptr, '$ref'))}"
        return None

    # ---- nodes and edges ---------------------------------------------------------------------

    def add(self, kind: str, ident, doc: Document, ptr: str, data: dict, name: str | None = None) -> tuple:
        key = (kind, ident)
        if key not in self.pending:
            self.pending[key] = {"doc": doc, "ptr": ptr, "data": data, "name": name}
        return key

    def edge(self, kind: str, source: tuple, target: tuple | None, **data):
        if target is not None:
            self.edges.append((kind, source, target, data))

    def location(self, doc: Document, ptr: str) -> str:
        return f"{doc.name}#{ptr}"

    # ---- schemas -----------------------------------------------------------------------------

    def schema(self, value, doc: Document, ptr: str, force=False) -> tuple | None:
        """The schema node for the schema at `ptr`, following a plain `$ref` to its target.

        `force` makes a node even for a plain reference, for a named component that is an alias of another."""
        if isinstance(value, bool) or value is None:
            return None
        if not isinstance(value, dict):
            self.issues.append(Issue(ERROR, "not-a-schema", "expected a schema", doc.name, self.line(doc, ptr)))
            return None
        if not force:
            found = self.deref(value, doc, ptr, is_schema=True)
            if found is None:
                return None
            value, doc, ptr = found
            if not isinstance(value, dict):
                return self.schema(value, doc, ptr)
        key = (g.SCHEMA, self.location(doc, ptr))
        if key not in self.pending:
            data = _copy(value, SCHEMA_DATA)
            for keyword in ("additionalProperties", "unevaluatedProperties"):
                if value.get(keyword) is False:  # a constraint, though not a schema of its own
                    data[keyword] = False
            self.add(g.SCHEMA, key[1], doc, ptr, data)
            self.required[key] = set()
            self.queue.append((key, value, doc, ptr))
        return key

    def walk_schema(self, owner: tuple, value: dict, doc: Document, ptr: str):
        """Everything one schema says, folding inline allOf branches into `owner`."""
        if "$ref" in value and isinstance(value["$ref"], str):
            target = self.deref({"$ref": value["$ref"]}, doc, ptr, is_schema=True)
            if target is not None:
                self.edge(g.INHERITS, owner, self.schema(*target), via="$ref", ref=self.ref_at(value, doc, ptr))
            if self.version == "3.0":
                return  # everything else beside the $ref is ignored
        if isinstance(value.get("$dynamicRef"), str) and self.version != "3.0":
            self.dynamic_ref(owner, value, doc, ptr)
        self.check_keywords(value, doc, ptr)
        if "$schema" in value and value["$schema"] not in KNOWN_DIALECTS:
            self.issues.append(Issue(ERROR, "unknown-dialect", f"unknown schema dialect {value['$schema']!r}",
                                     doc.name, self.line(doc, pointer.join(ptr, "$schema"))))

        self.required[owner].update(r for r in value.get("required", ()) if isinstance(r, str))
        for name, sub in _items(value.get("properties")):
            self.field(owner, name, sub, doc, pointer.join(ptr, "properties", name))

        for i, sub in enumerate(_list(value.get("allOf"))):
            sub_ptr = pointer.join(ptr, "allOf", i)
            if isinstance(sub, dict) and "$ref" in sub and self.is_pure_ref(sub, doc, sub_ptr):
                self.edge(g.INHERITS, owner, self.schema(sub, doc, sub_ptr), via="allOf", index=i,
                          ref=self.ref_at(sub, doc, sub_ptr))
            elif isinstance(sub, dict):
                self.merged.add((doc.name, sub_ptr))
                self.merged_into[(g.SCHEMA, self.location(doc, sub_ptr))] = (owner, i)
                self.walk_schema(owner, sub, doc, sub_ptr)

        for keyword in ("oneOf", "anyOf"):
            for i, sub in enumerate(_list(value.get(keyword))):
                sub_ptr = pointer.join(ptr, keyword, i)
                self.edge(g.OPTION, owner, self.schema(sub, doc, sub_ptr), keyword=keyword, index=i,
                          ref=self.ref_at(sub, doc, sub_ptr))
        if isinstance(value.get("discriminator"), dict):
            self.pending[owner]["data"].setdefault("discriminators", []).append(
                {"value": value["discriminator"], "doc": doc, "ptr": pointer.join(ptr, "discriminator")})

        def child(kind, keyword, *tokens, **data):
            sub_ptr = pointer.join(ptr, keyword, *tokens)
            sub = pointer.lookup(value, pointer.join("", keyword, *tokens))
            self.edge(kind, owner, self.schema(sub, doc, sub_ptr), keyword=keyword,
                      ref=self.ref_at(sub, doc, sub_ptr), **data)

        for keyword in ("if", "then", "else"):
            if keyword in value:
                child(g.CONDITIONAL, keyword)
        for name, _ in _items(value.get("dependentSchemas")):
            child(g.CONDITIONAL, "dependentSchemas", name, when=name)
        if "not" in value:
            child(g.NOT, "not")
        if isinstance(value.get("items"), dict):
            child(g.ITEMS, "items")
        for i, _ in enumerate(_list(value.get("prefixItems"))):
            child(g.ITEMS, "prefixItems", i, index=i)
        for keyword in ("contains", "unevaluatedItems"):
            if isinstance(value.get(keyword), dict):
                child(g.ITEMS, keyword)
        for keyword in ("additionalProperties", "unevaluatedProperties", "propertyNames"):
            if isinstance(value.get(keyword), dict):
                child(g.VALUES, keyword)
        for name, _ in _items(value.get("patternProperties")):
            child(g.VALUES, "patternProperties", name, pattern=name)

    def dynamic_ref(self, owner: tuple, value: dict, doc: Document, ptr: str):
        """`$dynamicRef` resolves like `$ref`, unless it lands on a `$dynamicAnchor` of the same name: then the
        target is chosen while validating, among the schemas with that dynamic anchor. Statically there is one
        target, kept like a `$ref`, or several, each kept as a condition (`when` is where it is), so their
        fields are never merged into "all fields"."""
        found = self.resolve_ref(doc, ptr, "$dynamicRef")
        if found is None:
            return
        target_doc, target_ptr = found
        name = urldefrag(value["$dynamicRef"])[1]
        landed = pointer.lookup(target_doc.data, target_ptr)
        candidates = [found]
        if isinstance(landed, dict) and landed.get("$dynamicAnchor") == name:
            # Only a resource whose references lead here can be in the dynamic scope when this is evaluated.
            here = self.base_at(doc, ptr)
            candidates += sorted(((c_doc, c_ptr) for c_doc, c_ptr, base in self.dynamic.get(name, ())
                                  if (c_doc, c_ptr) != found and self.reaches(base, here)),
                                 key=lambda c: (c[0].name, c[1]))
        ref = f"{doc.name}:{self.line(doc, pointer.join(ptr, '$dynamicRef'))}"
        for c_doc, c_ptr in candidates:
            target = self.schema(pointer.lookup(c_doc.data, c_ptr), c_doc, c_ptr)
            if len(candidates) == 1:
                self.edge(g.INHERITS, owner, target, via="$dynamicRef", ref=ref)
            else:
                self.edge(g.CONDITIONAL, owner, target, keyword="$dynamicRef", when=self.location(c_doc, c_ptr),
                          ref=ref)

    def reaches(self, start: str, goal: str) -> bool:
        """Whether references lead from one schema resource to another (or it is the same one)."""
        seen, todo = {start}, [start]
        while todo:
            current = todo.pop()
            if current == goal:
                return True
            for nxt in self.links.get(current, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return False

    def refers(self, value) -> bool:
        """Whether a schema is a reference: `$ref`, or in 3.1 `$dynamicRef`."""
        return isinstance(value, dict) and ("$ref" in value or (self.version != "3.0" and "$dynamicRef" in value))

    def check_keywords(self, value: dict, doc: Document, ptr: str):
        """Mistakes in a schema that speclens reads past but other tools refuse."""
        if self.version == "3.0":
            later = sorted(k for k in ("$id", "$anchor", "$dynamicRef", "$dynamicAnchor") if k in value)
            if later and (doc.name, ptr, "2020-12") not in self.warned:
                self.warned.add((doc.name, ptr, "2020-12"))
                self.issues.append(Issue(WARNING, "unsupported-keyword", f"{', '.join(later)}: JSON Schema 2020-12, "
                                         "which OpenAPI 3.0 does not have; ignored", doc.name, self.line(doc, ptr)))
        if self.version == "3.1" and "examples" in value and not isinstance(value["examples"], list):
            ex_ptr = pointer.join(ptr, "examples")
            if (doc.name, ex_ptr) not in self.warned:
                self.warned.add((doc.name, ex_ptr))
                self.issues.append(Issue(WARNING, "invalid-keyword", "a schema's `examples` must be an array in "
                                         "OpenAPI 3.1 (JSON Schema 2020-12); other tools may refuse this file",
                                         doc.name, self.line(doc, ex_ptr)))

    def field(self, owner: tuple, name: str, value, doc: Document, ptr: str):
        key = (g.FIELD, self.location(doc, ptr))
        if isinstance(value, dict):
            self.check_keywords(value, doc, ptr)
        if key not in self.pending:
            data = _copy(value, SCHEMA_DATA) if isinstance(value, dict) else {"schema": value}
            data["name"] = name
            self.add(g.FIELD, key[1], doc, ptr, data)
            if isinstance(value, dict) and (self.refers(value) or STRUCTURAL & set(value)):
                self.edge(g.FIELD_TYPE, key, self.schema(value, doc, ptr), ref=self.ref_at(value, doc, ptr))
        self.edge(g.HAS_FIELD, owner, key, inherited=False, via=[])

    # ---- operations --------------------------------------------------------------------------

    def path_item(self, value, doc: Document, ptr: str, path: str):
        found = self.deref(value, doc, ptr)
        if found is None or not isinstance(found[0], dict):
            return
        item, item_doc, item_ptr = found
        self.path_items.setdefault((item_doc.name, item_ptr), set()).add(path)
        shared = self.parameters(item.get("parameters"), item_doc, pointer.join(item_ptr, "parameters"), "path")
        for method in METHODS:
            op, op_doc, op_ptr = item.get(method), item_doc, pointer.join(item_ptr, method)
            if isinstance(op, dict) and "$ref" in op:
                # Not allowed by OpenAPI, but common in specs split into files: follow it, and say so.
                self.issues.append(Issue(WARNING, "nonstandard-ref", "OpenAPI does not allow $ref for an "
                                         "operation; followed anyway", op_doc.name,
                                         self.line(op_doc, pointer.join(op_ptr, "$ref"))))
                found = self.deref(op, op_doc, op_ptr)
                if found is None:
                    continue
                op, op_doc, op_ptr = found
            if not isinstance(op, dict):
                continue
            name = f"{method.upper()} {path}"
            if op_doc is not item_doc or not op_ptr.startswith(item_ptr):
                self.op_locations.setdefault((op_doc.name, op_ptr), set()).add(name)
            data = {"method": method.upper(), "path": path, **_copy(op, ("operationId", "summary", "description",
                                                                         "tags", "deprecated")),
                    "responses": [str(k) for k in (op.get("responses") or {})]}
            # The operation's own `security`, else the root document's; absent when neither declares one.
            security = _security(op["security"] if "security" in op else (doc.data or {}).get("security"))
            if security is not None:
                data["security"] = security
                self.used_schemes.update(k for way in security for k in way)
                where = pointer.join(op_ptr, "security") if "security" in op else "/security"
                for scheme in sorted({k for way in security for k in way} - set(self.security_schemes)):
                    if (doc.name, where, scheme) not in self.warned:
                        self.warned.add((doc.name, where, scheme))
                        at = op_doc if "security" in op else doc
                        self.issues.append(Issue(WARNING, "unknown-security-scheme", f"security names {scheme!r}, "
                                                 "which components/securitySchemes does not define", at.name,
                                                 self.line(at, where)))
            key = self.add(g.OPERATION, name, op_doc, op_ptr, name=name, data=data)
            params = dict(shared)
            params.update(self.parameters(op.get("parameters"), op_doc, pointer.join(op_ptr, "parameters"),
                                          "operation"))
            for (where, pname), (param_key, level) in params.items():
                self.edge(g.TAKES, key, param_key, **{"in": where}, name=pname, level=level)
            self.request_body(key, op.get("requestBody"), op_doc, pointer.join(op_ptr, "requestBody"))
            for status, response in _items(op.get("responses")):
                self.response(key, str(status), response, op_doc, pointer.join(op_ptr, "responses", status))
            self.callbacks(key, op.get("callbacks"), op_doc, pointer.join(op_ptr, "callbacks"))

    def callbacks(self, op: tuple, values, doc: Document, ptr: str):
        """The requests an operation makes the API send back: each callback's name, the expression giving its
        URL, its method and summary on the operation, and an edge to the schema of each body it sends."""
        for name, value in _items(values):
            found = self.deref(value, doc, pointer.join(ptr, name))
            if found is None or not isinstance(found[0], dict):
                continue
            cb, cb_doc, cb_ptr = found
            for expression, item in _items(cb):
                found = self.deref(item, cb_doc, pointer.join(cb_ptr, expression))
                if found is None or not isinstance(found[0], dict):
                    continue
                item, i_doc, i_ptr = found
                for method in METHODS:
                    request = item.get(method)
                    if not isinstance(request, dict):
                        continue
                    self.pending[op]["data"].setdefault("callbacks", []).append(
                        {"name": name, "expression": expression, "method": method.upper(),
                         **_copy(request, ("summary", "description")),
                         "responses": [str(k) for k in (request.get("responses") or {})]})
                    body_ptr = pointer.join(i_ptr, method, "requestBody")
                    found = self.deref(request.get("requestBody"), i_doc, body_ptr)
                    if found is None or not isinstance(found[0], dict):
                        continue
                    body, b_doc, b_ptr = found
                    for media_type, media, m_ptr in self.media(body.get("content"), b_doc,
                                                               pointer.join(b_ptr, "content")):
                        target = self.schema(media["schema"], b_doc, pointer.join(m_ptr, "schema"))
                        self.edge(g.CALLBACK, op, target, callback=name, method=method.upper(),
                                  media_type=media_type)

    def parameters(self, values, doc: Document, ptr: str, level: str) -> dict:
        """(in, name) -> (parameter node key, level), as OpenAPI identifies a parameter by both."""
        out = {}
        for i, value in enumerate(_list(values)):
            key = self.parameter(value, doc, pointer.join(ptr, i))
            if key is not None:
                data = self.pending[key]["data"]
                out[(data.get("in"), data.get("name"))] = (key, level)
        return out

    def parameter(self, value, doc: Document, ptr: str) -> tuple | None:
        found = self.deref(value, doc, ptr)
        if found is None or not isinstance(found[0], dict):
            return None
        value, doc, ptr = found
        key = (g.PARAMETER, self.location(doc, ptr))
        if key in self.pending:
            return key
        data = _copy(value, ("name", "in", "description", "required", "deprecated", "style", "explode"))
        schema = value.get("schema")
        if isinstance(schema, dict):
            self.check_keywords(schema, doc, pointer.join(ptr, "schema"))
        if isinstance(schema, dict) and not (self.refers(schema) or STRUCTURAL & set(schema)):
            data.update(_copy(schema, SCHEMA_DATA))
        self.add(g.PARAMETER, key[1], doc, ptr, data)
        if isinstance(schema, dict) and (self.refers(schema) or STRUCTURAL & set(schema)):
            s_ptr = pointer.join(ptr, "schema")
            self.edge(g.PARAM_TYPE, key, self.schema(schema, doc, s_ptr), ref=self.ref_at(schema, doc, s_ptr))
        for media_type, media, m_ptr in self.media(value.get("content"), doc, pointer.join(ptr, "content")):
            s_ptr = pointer.join(m_ptr, "schema")
            self.edge(g.PARAM_TYPE, key, self.schema(media.get("schema"), doc, s_ptr), media_type=media_type)
        return key

    def media(self, content, doc: Document, ptr: str):
        for media_type, media in _items(content):
            if isinstance(media, dict) and "schema" in media:
                yield media_type, media, pointer.join(ptr, media_type)

    def request_body(self, op: tuple | None, value, doc: Document, ptr: str):
        found = self.deref(value, doc, ptr) if value is not None else None
        if found is None or not isinstance(found[0], dict):
            return
        body, doc, ptr = found
        for media_type, media, m_ptr in self.media(body.get("content"), doc, pointer.join(ptr, "content")):
            s_ptr = pointer.join(m_ptr, "schema")
            target = self.schema(media["schema"], doc, s_ptr)
            if op is not None:
                self.edge(g.ACCEPTS, op, target, media_type=media_type, required=bool(body.get("required")),
                          ref=self.ref_at(media["schema"], doc, s_ptr))

    def response(self, op: tuple | None, status: str, value, doc: Document, ptr: str):
        found = self.deref(value, doc, ptr)
        if found is None or not isinstance(found[0], dict):
            return
        response, doc, ptr = found
        for name, link in _items(response.get("links")):
            found = self.deref(link, doc, pointer.join(ptr, "links", name))
            if op is not None and found is not None and isinstance(found[0], dict):
                self.pending[op]["data"].setdefault("links", []).append(
                    {"status": status, "name": name,
                     **_copy(found[0], ("operationId", "operationRef", "parameters", "requestBody", "description"))})
        for media_type, media, m_ptr in self.media(response.get("content"), doc, pointer.join(ptr, "content")):
            s_ptr = pointer.join(m_ptr, "schema")
            target = self.schema(media["schema"], doc, s_ptr)
            if op is not None:
                self.edge(g.RETURNS, op, target, status=status, media_type=media_type,
                          ref=self.ref_at(media["schema"], doc, s_ptr))
        for header, h_value in _items(response.get("headers")):
            h_found = self.deref(h_value, doc, pointer.join(ptr, "headers", header))
            if h_found is None or not isinstance(h_found[0], dict) or "schema" not in h_found[0]:
                continue
            h, h_doc, h_ptr = h_found
            target = self.schema(h["schema"], h_doc, pointer.join(h_ptr, "schema"))
            if op is not None:
                self.edge(g.RETURNS, op, target, status=status, header=header)

    def components(self, doc: Document):
        comps = doc.data.get("components") if isinstance(doc.data, dict) else None
        if not isinstance(comps, dict):
            return
        base = "/components"
        for kind in ("parameters", "requestBodies", "responses", "headers", "pathItems", "callbacks"):
            for name, value in _items(comps.get(kind)):
                where = self.deref(value, doc, pointer.join(base, kind, name)) if isinstance(value, dict) \
                    and "$ref" in value else None
                if where is not None and where[1] is not doc:
                    self.component_at.setdefault((where[1].name, where[2]), f"{kind}/{name}")
        for name, value in _items(comps.get("schemas")):
            ptr = pointer.join(base, "schemas", name)
            key = self.schema(value, doc, ptr, force=True)
            if key is not None and isinstance(value, dict) and "$ref" in value and self.is_pure_ref(value, doc, ptr):
                target = self.schema(value, doc, ptr)
                if target is not None and target != key:
                    self.alias_of[key] = target
        for name, value in _items(comps.get("parameters")):
            self.parameter(value, doc, pointer.join(base, "parameters", name))
        for name, value in _items(comps.get("requestBodies")):
            self.request_body(None, value, doc, pointer.join(base, "requestBodies", name))
        for name, value in _items(comps.get("responses")):
            self.response(None, "", value, doc, pointer.join(base, "responses", name))
        for name, value in _items(comps.get("headers")):
            if isinstance(value, dict) and "schema" in value:
                self.schema(value["schema"], doc, pointer.join(base, "headers", name, "schema"))

    # ---- the whole spec ----------------------------------------------------------------------

    def run(self) -> Graph:
        if not self.roots:
            self.issues.append(Issue(ERROR, "no-root", "no OpenAPI root file was given or found", ""))
        roots = []
        for root_path in self.roots:
            root = self.document(root_path)
            if not isinstance(root.data, dict):
                if root.data is not None:
                    self.issues.append(Issue(ERROR, "not-openapi", "the root file is not a mapping", root.name))
            elif self.check_version(root):
                roots.append(root)
                continue
            self.walked_components.add(root.path)  # ignored: nothing in it is part of the spec
        self.crawl(roots)
        for root in roots:  # the names a security requirement uses are those of its root document
            schemes = root.data.get("components")
            for name, value in _items(schemes.get("securitySchemes") if isinstance(schemes, dict) else None):
                ptr = pointer.join("/components/securitySchemes", name)
                found = self.deref(value, root, ptr)
                if found and isinstance(found[0], dict) and name not in self.security_schemes:
                    self.security_schemes[name] = {**_scheme(found[0]), "file": found[1].name,
                                                   "line": self.line(found[1], found[2])}
        seen: dict[str, tuple[str, str]] = {}  # path -> where its path item is, for paths in several roots
        for root in roots:
            for section, prefix in (("paths", ""), ("webhooks", "webhook:")):
                for name, item in _items(root.data.get(section)):
                    ptr = pointer.join(f"/{section}", name)
                    found = self.deref(item, root, ptr)
                    where = (found[1].name, found[2]) if found else (root.name, ptr)
                    path = prefix + name
                    if path in seen:
                        if seen[path] != where:  # the same path item reached from two roots is not a conflict
                            self.issues.append(Issue(ERROR, "duplicate-path", f"{path} is also defined in "
                                                     f"{seen[path][0]}; this one is ignored", root.name,
                                                     self.line(root, ptr)))
                        continue
                    seen[path] = where
                    self.path_item(item, root, ptr, path)
        for name in sorted(set(self.security_schemes) - self.used_schemes):
            scheme = self.security_schemes[name]
            self.issues.append(Issue(WARNING, "unused-security-scheme", f"security scheme {name!r} is defined, but "
                                     "no operation's security requirement names it, so nothing says it is needed",
                                     scheme["file"], scheme["line"]))
        while True:
            self.drain()
            new = [d for p, d in self.docs.items() if p not in self.walked_components]
            if not new:
                break
            for doc in new:
                self.walked_components.add(doc.path)
                if doc.data is not None:
                    self.index(doc)
                    self.components(doc)
                    self.scan_refs(doc, doc.data, "")
        return self.finish()

    def crawl(self, roots: list[Document]):
        """Load and index every file the roots' references reach, before anything is built, so a reference to
        an `$id` finds it whichever file declares it, and in whatever order the files are walked."""
        todo, done = list(roots), set()
        while todo:
            doc = todo.pop()
            if doc.path in done or doc.data is None:
                continue
            done.add(doc.path)
            self.index(doc)
            known = set(self.docs)
            for ptr, keyword in _references(doc.data, "", self.version):
                self.locate(doc, ptr, pointer.lookup(doc.data, pointer.join(ptr, keyword)))
            todo.extend(d for p, d in self.docs.items() if p not in known)

    def check_version(self, root: Document) -> bool:
        version = str(root.data.get("openapi", ""))
        minor = "3.0" if version.startswith("3.0") else "3.1" if version.startswith("3.1") else None
        if minor and self.version and minor != self.version:
            # 3.0 and 3.1 read schemas by different rules, so one spec cannot hold both.
            self.issues.append(Issue(ERROR, "version-mismatch", f"openapi {version} differs from the other root "
                                     f"files ({self.version}); this file is ignored", root.name,
                                     self.line(root, "/openapi")))
            return False
        if minor == "3.0":
            self.version = "3.0"
        elif minor == "3.1":
            self.version = "3.1"
            dialect = root.data.get("jsonSchemaDialect")
            if dialect is not None and dialect not in KNOWN_DIALECTS:
                self.issues.append(Issue(ERROR, "unknown-dialect", f"unknown schema dialect {dialect!r}",
                                         root.name, self.line(root, "/jsonSchemaDialect")))
        else:
            found = f"openapi: {version}" if version else ("swagger: " + str(root.data["swagger"])
                                                         if "swagger" in root.data else "no openapi field")
            self.issues.append(Issue(ERROR, "unsupported-version",
                                     f"only OpenAPI 3.0 and 3.1 are supported ({found})", root.name, 1))
            return False
        return True

    def drain(self):
        while self.queue:
            key, value, doc, ptr = self.queue.popleft()
            self.walk_schema(key, value, doc, ptr)

    def scan_refs(self, doc: Document, value, ptr: str):
        """Resolve every reference in a file, including ones the graph does not use, so none goes unchecked."""
        for ref_ptr, keyword in _references(value, ptr, self.version):
            self.resolve_ref(doc, ref_ptr, keyword)

    # ---- after the walk ----------------------------------------------------------------------

    def finish(self) -> Graph:
        self.drain()
        discriminator_values = self.discriminators()
        self.drain()  # a mapping can name a schema nothing else refers to
        self.link_inline_types()
        for key, (owner, index) in self.merged_into.items():
            if key in self.pending:
                # Folded into its schema, and also referred to on its own: keep the two linked.
                self.edge(g.INHERITS, owner, key, via="allOf", index=index)
        self.drop_same_named_aliases()
        ids = self.names()
        graph = Graph(self.label)
        for key, p in self.pending.items():
            doc, ptr = p["doc"], p["ptr"]
            span = doc.lines.get(ptr, (None, None))
            data = {k: v for k, v in p["data"].items() if k != "discriminators"}
            if key[0] == g.SCHEMA:
                data["required"] = sorted(self.required.get(key, ()))
                data["named"] = key in self.named  # a component or a file of its own, not written inline
                if key in self.mappings:
                    prop, mapping = self.mappings[key]
                    data["discriminator"] = {"propertyName": prop, "mapping": {
                        value: ids[self.dropped_aliases.get(t, t)] for value, t in mapping.items()
                        if self.dropped_aliases.get(t, t) in ids}}
            name = ids[key][len(f"{self.label}:{key[0]}/"):]
            graph.add_node(Node(id=ids[key], label=self.label, kind=key[0], name=name,
                                location=self.location(doc, ptr), file=doc.name, line_start=span[0],
                                line_end=span[1], data=data))
        seen = set()
        for kind, source, target, data in self.edges:
            edge_key = (kind, source, target, repr(sorted(data.items())))
            if edge_key in seen:
                continue
            seen.add(edge_key)
            if kind == g.OPTION and (source, target) in discriminator_values:
                data = {**data, **discriminator_values[(source, target)]}
            graph.add_edge(Edge(kind, ids[source], ids[target], data))
        for node in graph.of_kind(g.FIELD):
            owners = graph.incoming(node.id, g.HAS_FIELD)
            node.data["required"] = any(node.data["name"] in graph.nodes[e.source].data["required"]
                                        for e in owners)
        self.inherit(graph)
        graph.security_schemes = self.security_schemes
        return graph

    def drop_same_named_aliases(self):
        """A component that only points at a schema named the same (`Facility: {$ref: './facilities.yaml#/Facility'}`,
        `Pet: {$ref: './schemas/Pet.yaml'}`) is that schema, not a second one: keep one node, under that name."""
        self.node_roots = {(p["doc"].name, p["ptr"]) for p in self.pending.values() if p["ptr"] == ""}

        def name(key):  # the name each would have on its own, before collisions are settled
            p = self.pending[key]
            _, base, suffix = self.name_parts(key[0], p["doc"], p["ptr"])
            return base + suffix

        same = {alias: target for alias, target in self.alias_of.items()
                if alias in self.pending and target in self.pending and name(alias) == name(target)}
        if not same:
            return
        self.dropped_aliases = same
        for alias in same:
            del self.pending[alias]
            self.required.pop(alias, None)
        self.edges = [(kind, same.get(source, source), same.get(target, target), data)
                      for kind, source, target, data in self.edges]
        self.edges = [e for e in self.edges if e[1] != e[2] or e[0] != g.INHERITS]

    def link_inline_types(self):
        """A field or parameter with a simple inline type gets no schema node of its own. If something elsewhere
        refers to that inline schema, a node exists there after all: link it, so used-by finds the field."""
        typed = {source for kind, source, _, _ in self.edges if kind in (g.FIELD_TYPE, g.PARAM_TYPE)}
        for key, p in list(self.pending.items()):
            if key in typed or key[0] not in (g.FIELD, g.PARAMETER):
                continue
            ptr = p["ptr"] if key[0] == g.FIELD else pointer.join(p["ptr"], "schema")
            target = (g.SCHEMA, self.location(p["doc"], ptr))
            if target in self.pending:
                self.edge(g.FIELD_TYPE if key[0] == g.FIELD else g.PARAM_TYPE, key, target)

    def discriminators(self) -> dict:
        """(schema, option) -> {"discriminator": property, "values": [...]}, explicit mapping or implicit name."""
        out = {}
        for key, p in self.pending.items():
            for d in p["data"].get("discriminators", ()):
                disc, doc, d_ptr = d["value"], d["doc"], d["ptr"]
                mapped = {}
                for value, target in _items(disc.get("mapping")):
                    t_key = self.mapping_target(str(target), doc, pointer.join(d_ptr, "mapping", value))
                    if t_key is not None:
                        mapped.setdefault(t_key, []).append(str(value))
                        self.mappings.setdefault(key, (disc.get("propertyName"), {}))[1][str(value)] = t_key
                for kind, source, target, _ in self.edges:
                    if kind != g.OPTION or source != key:
                        continue
                    values = mapped.get(target)
                    if values is None:
                        tokens = pointer.split(self.pending[target]["ptr"])
                        values = [tokens[2]] if tokens[:2] == ["components", "schemas"] and len(tokens) == 3 else []
                    out[(source, target)] = {"discriminator": disc.get("propertyName"), "values": values}
        return out

    def mapping_target(self, target: str, doc: Document, ptr: str) -> tuple | None:
        bare = not ("/" in target or "#" in target or target.endswith((".yaml", ".yml", ".json")))
        # A bare name is a schema in the OpenAPI document's components, whatever $id is around the mapping.
        holder = {"$ref": f"#/components/schemas/{target}" if bare else target}
        found = self._resolve_value(holder, doc, ptr, doc.path.as_uri() if bare else None)
        return self.schema(*found) if found else None

    def _resolve_value(self, holder: dict, doc: Document, ptr: str, base: str | None = None):
        # A mapping value is a reference written as a plain string; resolve it as if it were a `$ref` there.
        found, problem = self.locate(doc, ptr, holder["$ref"], base)
        if found is None:
            self.issues.append(Issue(ERROR, "unresolved-ref", f"discriminator mapping {holder['$ref']!r} "
                                     f"not found: {problem[1]}", doc.name, self.line(doc, ptr)))
            return None
        return pointer.lookup(found[0].data, found[1]), found[0], found[1]

    def inherit(self, graph: Graph):
        """Inherited fields: has_field edges from each schema to every field of every allOf ancestor,
        `required` as the union over the whole chain, aliases for effective addresses, contradictions."""
        for schema in graph.of_kind(g.SCHEMA):
            chain = []  # (ancestor id, path from schema)
            seen = {schema.id}
            frontier = deque([(schema.id, [])])
            while frontier:
                current, path = frontier.popleft()
                for e in graph.outgoing(current, g.INHERITS):
                    if e.target not in seen:
                        seen.add(e.target)
                        chain.append((e.target, path + [e.target]))
                        frontier.append((e.target, path + [e.target]))
            required = set(schema.data["required"])
            for ancestor, _ in chain:
                required.update(graph.nodes[ancestor].data["required"])
            schema.data["effective_required"] = sorted(required)
            by_name: dict[str, list[str]] = {}
            for e in graph.outgoing(schema.id, g.HAS_FIELD):
                e.data["required"] = graph.nodes[e.target].data["name"] in required
                by_name.setdefault(graph.nodes[e.target].data["name"], []).append(e.target)
            for ancestor, path in chain:
                for e in graph.outgoing(ancestor, g.HAS_FIELD):
                    if e.data["inherited"]:
                        continue
                    name = graph.nodes[e.target].data["name"]
                    if e.target in by_name.get(name, ()):
                        continue
                    graph.add_edge(Edge(g.HAS_FIELD, schema.id, e.target,
                                        {"inherited": True, "via": path, "required": name in required}))
                    by_name.setdefault(name, []).append(e.target)
            for name, field_ids in by_name.items():
                address = f"{self.label}:{g.FIELD}/{schema.name}.{name}"
                if address not in graph.nodes or field_ids != [address]:
                    graph.aliases[address] = field_ids
                self.contradiction(graph, schema, name, field_ids)

    def contradiction(self, graph: Graph, schema: Node, name: str, field_ids: list[str]):
        types = []
        for fid in field_ids:
            t = graph.nodes[fid].data.get("type")
            if t is not None:
                types.append((fid, set(t) if isinstance(t, list) else {t}))
        for i in range(len(types)):
            for j in range(i + 1, len(types)):
                a, b = _widen(types[i][1]), _widen(types[j][1])
                if not a & b:
                    self.issues.append(Issue(
                        WARNING, "contradiction",
                        f"{schema.name}.{name} is {'/'.join(sorted(types[i][1]))} in {types[i][0]} and "
                        f"{'/'.join(sorted(types[j][1]))} in {types[j][0]}; no value can satisfy both",
                        schema.file, schema.line_start))

    # ---- names -------------------------------------------------------------------------------

    def names(self) -> dict[tuple, str]:
        """key -> id. A name comes from where the node was written: `components/schemas/Pet` is `Pet`,
        its property `weight` is `Pet.weight`. When two places give the same name, a fixed rule decides:
        first the colliding roots (two files each defining `Pet`) are named by location. Then, when two allOf
        branches define the same property, the branch stays in the name (`Both#allOf/1.amount`). Anything still
        colliding is named by location. Either way the result does not depend on the order files were read."""
        self.node_roots = {(p["doc"].name, p["ptr"]) for p in self.pending.values() if p["ptr"] == ""}
        folded = self.full_names(fold=True)
        names = dict(folded)
        for key in self.colliding(folded):
            names[key] = None
        unfolded = self.full_names(fold=False)
        for key, name in names.items():
            if name is None:
                names[key] = unfolded[key]
        still = self.colliding(names)
        out = {}
        for key, name in names.items():
            if key in still:
                p = self.pending[key]
                name = self.location(p["doc"], p["ptr"])
            out[key] = f"{self.label}:{key[0]}/{name}"
        return out

    @staticmethod
    def colliding(names: dict[tuple, str]) -> set[tuple]:
        groups: dict[tuple, list] = {}
        for key, name in names.items():
            groups.setdefault((key[0], name), []).append(key)
        return {key for keys in groups.values() if len(keys) > 1 for key in keys}

    def full_names(self, fold: bool) -> dict[tuple, str]:
        """key -> name, roots made unique. `fold` leaves inline allOf branches out of names."""
        self.fold = fold
        parts = {}
        for key, p in self.pending.items():
            if key[0] != g.OPERATION:
                parts[key] = self.name_parts(key[0], p["doc"], p["ptr"])
        bases: dict[str, set] = {}
        for base_key, base_name, _ in parts.values():
            bases.setdefault(base_name, set()).add(base_key)
        names = {}
        if fold:
            # A schema of its own: a component schema, a $defs entry or a file. A component header, parameter
            # or response is named too, but what it holds is not a schema anyone refers to by that name.
            self.named = {key for key, (_, base, suffix) in parts.items()
                          if key[0] == g.SCHEMA and not suffix and not base.startswith(_NOT_SCHEMAS)}
        for key, (base_key, base_name, suffix) in parts.items():
            if len(bases[base_name]) > 1:
                base_name = f"{base_key[0]}#{base_key[1]}"
            if key[0] == g.PARAMETER and base_name.startswith("parameters/") and not suffix:
                base_name = base_name[len("parameters/"):]  # a component parameter is named as written
            names[key] = base_name + suffix
        for key, p in self.pending.items():
            if key[0] == g.OPERATION:
                names[key] = p["name"]
        return names

    def name_parts(self, kind: str, doc: Document, ptr: str):
        """(base key, base name, suffix). The base is the named thing a node sits in; the suffix is the way
        from there to the node."""
        t = pointer.split(ptr)
        prefixes = [""]  # prefixes[i] is the pointer of the first i tokens
        for token in t:
            prefixes.append(pointer.join(prefixes[-1], token))
        for i in range(len(t), -1, -1):
            # Inside an operation kept in a file of its own: named by the operation.
            names = self.op_locations.get((doc.name, prefixes[i]))
            if names and len(names) == 1:
                base_ptr = prefixes[i]
                return (doc.name, base_ptr), next(iter(names)), self.suffix(doc, t, i, base_ptr)
            # Inside a path item kept elsewhere (another file, components/pathItems): named by its path.
            paths = self.path_items.get((doc.name, prefixes[i]))
            if paths and len(paths) == 1 and ptr != prefixes[i]:
                path = next(iter(paths))
                if i < len(t) and t[i] in METHODS:
                    base, i = f"{t[i].upper()} {path}", i + 1
                else:
                    base = path
                base_ptr = pointer.join("", *t[:i])
                return (doc.name, base_ptr), base, self.suffix(doc, t, i, base_ptr)
        for i in range(len(t), 0, -1):  # a component kept in another file: named as the component
            kind = self.component_at.get((doc.name, prefixes[i]))
            if kind:
                base_ptr = prefixes[i]
                return (doc.name, base_ptr), kind, self.suffix(doc, t, i, base_ptr)
        if len(t) >= 3 and t[:2] == ["components", "schemas"]:
            base, i = t[2], 3
        elif len(t) >= 3 and t[0] == "components":
            base, i = f"{t[1]}/{t[2]}", 3
        elif len(t) >= 2 and t[0] in ("paths", "webhooks"):
            path = t[1] if t[0] == "paths" else f"webhook:{t[1]}"
            if len(t) >= 3 and t[2] in METHODS:
                base, i = f"{t[2].upper()} {path}", 3
            else:
                base, i = path, 2
        elif len(t) >= 2 and t[0] in ("$defs", "definitions"):
            base, i = t[1], 2
        elif not (isinstance(doc.data, dict) and "openapi" in doc.data):
            if t and (doc.name, "") not in self.node_roots:
                # A file holding a collection of named pieces (`parameters.yml#/limit`): named by the key.
                base, i = t[0], 1
            else:
                # A file holding one piece of a spec (a schema, a response): named by the file.
                base, i = Path(doc.name).stem, 0
        else:
            return (doc.name, ptr), self.location(doc, ptr), ""
        base_ptr = pointer.join("", *t[:i])
        return (doc.name, base_ptr), base, self.suffix(doc, t, i, base_ptr)

    def suffix(self, doc: Document, t: list[str], i: int, ptr: str) -> str:
        out = []
        while i < len(t):
            tok, nxt = t[i], (t[i + 1] if i + 1 < len(t) else None)
            step = 1
            if tok == "properties" and nxt is not None:
                out.append("." + nxt)
                step = 2
            elif tok == "allOf" and nxt is not None and i + 2 < len(t) and self.fold \
                    and (doc.name, pointer.join(ptr, tok, nxt)) in self.merged:
                step = 2  # an inline allOf branch is part of the schema holding it
            elif tok in METHODS and not out:
                out.append("#" + tok)
            elif tok == "responses" and nxt is not None:
                rest = t[i + 2:i + 4]
                if len(rest) == 2 and rest[0] in ("content", "headers"):
                    out.append(f"#response/{nxt}/" + (rest[1] if rest[0] == "content" else f"header/{rest[1]}"))
                    step = 4
                else:
                    out.append(f"#response/{nxt}")
                    step = 2
            elif tok == "requestBody":
                if t[i + 1:i + 2] == ["content"] and i + 2 < len(t):
                    out.append(f"#request/{t[i + 2]}")
                    step = 3
                else:
                    out.append("#request")
            elif tok == "parameters" and nxt is not None:
                try:
                    param = pointer.lookup(doc.data, pointer.join(ptr, tok, nxt))
                    out.append(f"#{param.get('in')}/{param.get('name')}")
                except (KeyError, AttributeError):
                    out.append(f"#parameters/{nxt}")
                step = 2
            elif tok in ("content", "headers") and nxt is not None:
                out.append(f"#{nxt}" if tok == "content" else f"#header/{nxt}")
                step = 2
            elif tok == "schema":
                pass
            elif tok in PAIRED and nxt is not None:
                out.append(f"#{tok}/{nxt}")
                step = 2
            else:
                out.append("#" + tok)
            ptr = pointer.join(ptr, *t[i:i + step])
            i += step
        return "".join(out)


_NOT_SCHEMAS = ("headers/", "parameters/", "responses/", "requestBodies/", "pathItems/", "callbacks/")


def _join(base: str, ref: str) -> str:
    """A reference read against a base URI. `urljoin` does not handle URNs (`urn:uuid:...#/x`)."""
    if urlsplit(ref).scheme:
        return ref
    if ref.startswith("#") or not ref:
        return urldefrag(base)[0] + ref
    return urljoin(base, ref)


def _references(value, ptr: str, version: str | None, names=False):
    """(pointer, keyword) of every `$ref` (and in 3.1 `$dynamicRef`) in a value, outside data such as examples.

    `names` is true for a map whose keys are names (`properties`, `schemas`, ...): a property called
    `default` is a schema, not a default value."""
    stack = [(value, ptr, names)]
    while stack:
        value, ptr, names = stack.pop()
        if isinstance(value, dict):
            if not names:
                if isinstance(value.get("$ref"), str):
                    yield ptr, "$ref"
                if isinstance(value.get("$dynamicRef"), str) and version != "3.0":
                    yield ptr, "$dynamicRef"
            children = []
            for k, v in value.items():
                if names:
                    children.append((v, pointer.join(ptr, k), False))
                elif k in LITERALS or k.startswith("x-") or (k == "examples" and isinstance(v, list)):
                    continue
                elif k == "examples" and isinstance(v, dict):
                    for name, example in v.items():  # an Example object's value is data: only its $ref
                        if isinstance(example, dict) and isinstance(example.get("$ref"), str):
                            yield pointer.join(ptr, k, name), "$ref"
                else:
                    children.append((v, pointer.join(ptr, k), k in NAME_MAPS))
            stack.extend(reversed(children))  # in the order written
        elif isinstance(value, list):
            stack.extend((v, pointer.join(ptr, i), False) for i, v in reversed(list(enumerate(value))))


def _items(value):
    return list(value.items()) if isinstance(value, dict) else []


def _list(value):
    return value if isinstance(value, list) else []


def _security(value) -> list[dict] | None:
    """A security requirement list, as [{scheme: [scopes]}]: any one entry is enough, `{}` allows anonymous
    access. None when nothing is declared."""
    if not isinstance(value, list):
        return None
    out = []
    for entry in value:
        if isinstance(entry, dict):
            out.append({str(k): sorted(map(str, v)) if isinstance(v, list) else [] for k, v in entry.items()})
    return out


def _scheme(value: dict) -> dict:
    """What a client must do to use a security scheme: its kind, where the credential goes, and for OAuth the
    flows and the scopes they offer."""
    out = {k: str(value[k]) for k in ("type", "in", "name", "scheme", "openIdConnectUrl") if k in value}
    flows = value.get("flows")
    if isinstance(flows, dict):
        out["flows"] = sorted(str(k) for k in flows)
        out["scopes"] = sorted({str(s) for f in flows.values() if isinstance(f, dict)
                                for s in (f.get("scopes") or {}) if isinstance(f.get("scopes"), dict)})
    return out


def _copy(value, keys) -> dict:
    return {k: value[k] for k in keys if isinstance(value, dict) and k in value}


def _widen(types: set) -> set:
    # Every integer is a number, so `integer` and `number` overlap.
    return types | ({"number"} if "integer" in types else set())


def build(roots: list[Path], label: str, allow: list | None = None) \
        -> tuple[Graph, dict[Path, Document], list[Issue], str | None]:
    builder = _Builder(roots, label, allow)
    graph = builder.run()
    issues = list(dict.fromkeys(builder.issues))  # the same problem reached two ways is one problem
    return graph, builder.docs, issues, builder.version
