"""A loaded spec, and several of them under labels."""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import graph as g
from . import compare as _compare
from . import query
from .build import build
from .graph import Graph
from .index import Indexes
from .issues import ERROR, Issue
from .loader import Document
from .text import label_key


@dataclass
class Spec:
    label: str
    root: Path  # the root file, or the folder holding several
    version: str | None  # "3.0" or "3.1"; None when the root is not a supported OpenAPI document
    graph: Graph
    documents: dict[Path, Document]
    issues: list[Issue]
    roots: list[Path] = field(default_factory=list)  # the root files read
    _index: Indexes | None = field(default=None, repr=False)

    @property
    def index(self) -> Indexes:
        """Built on first use."""
        if self._index is None:
            self._index = Indexes(self.graph)
        return self._index

    def get_schema(self, name: str, expand: int = 0, max_chars: int | None = None) -> query.SchemaResult:
        return query.get_schema(self, name, expand, max_chars)

    def get_operation(self, method: str, path: str | None = None, expand: int = 0, errors: bool = False,
                      max_chars: int | None = None) -> query.OperationResult:
        return query.get_operation(self, method, path, expand, errors, max_chars)

    def find_field(self, name: str) -> query.FieldResult:
        return query.find_field(self, name)

    def list_operations(self, filter: str | None = None, method: str | None = None,
                        tag: str | None = None) -> query.OperationList:
        return query.list_operations(self, filter, method, tag)

    def search(self, text: str, kinds=None, limit: int = 10) -> query.SearchResult:
        return query.search(self, text, kinds, limit)

    def names_in(self, text: str) -> query.Mentions:
        return query.names_in(self, text)

    def source_of(self, node_id: str) -> query.SourceResult:
        """Like `source`, as a result: the id, file:line and the original lines."""
        return query.source(self, node_id)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == ERROR]

    @property
    def ok(self) -> bool:
        return not self.errors

    def source(self, node_id: str) -> str:
        """The original lines behind a node."""
        node = self.graph.nodes[node_id]
        doc = next(d for d in self.documents.values() if d.name == node.file)
        return doc.source(node.location.split("#", 1)[1])

    def counts(self) -> dict[str, int]:
        return {"files": len(self.documents),
                "operations": len(self.graph.of_kind(g.OPERATION)),
                "schemas": len(self.graph.of_kind(g.SCHEMA)),
                "fields": len(self.graph.of_kind(g.FIELD)),
                "parameters": len(self.graph.of_kind(g.PARAMETER)),
                "edges": len(self.graph.edges)}


def check_label(label: str) -> str:
    if not label or ":" in label or "/" in label or label != label.strip():
        raise ValueError(f"a label must be non-empty, without ':' or '/' or surrounding spaces: {label!r}")
    return label


def load_spec(path, label: str | None = None, allow: list | None = None) -> Spec:
    """Load a spec from its root file, with every file it references.

    `path` can also be a folder, whose top-level files with an `openapi` (or `swagger`) key are the roots, or
    a list of root files: an API split into several root documents read as one spec. Their paths and
    components come together; a path defined in two roots, or roots of different OpenAPI versions, are
    reported. The label defaults to the roots' `info.version` when they agree, else the folder's or the
    root file's name.

    With `allow` (folders), no file outside them is read, not even one a `$ref` names: it is reported as
    `outside-allowed` instead."""
    if isinstance(path, (list, tuple)):
        roots = [Path(p) for p in path]
        root = Path(os.path.commonpath([p.resolve().parent for p in roots])) if len(roots) > 1 else \
            (roots[0] if roots else Path.cwd())
    elif Path(path).is_dir():
        root = Path(path)
        roots = find_roots(root)
    else:
        root = Path(path)
        roots = [root]
    if label is None:
        label = _default_label(roots, root)
    graph, documents, issues, version = build(roots, check_label(label), allow)
    return Spec(label=label, root=root.resolve(), version=version, graph=graph, documents=documents,
                issues=issues, roots=[r.resolve() for r in roots])


_ROOT_KEY = re.compile(r"""^["']?(?:openapi|swagger)["']?\s*:""", re.M)  # a top-level YAML key


def find_roots(folder: Path) -> list[Path]:
    """The files directly in `folder` that are OpenAPI documents, not fragments: a top-level `openapi` key.
    Hidden files (".name") are left out: they are not part of the API as published."""
    out = []
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() in (".yaml", ".yml", ".json") and p.is_file() and not p.name.startswith("."):
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if p.suffix.lower() == ".json":
                try:
                    data = json.loads(text)
                except ValueError:
                    continue
                if isinstance(data, dict) and ("openapi" in data or "swagger" in data):
                    out.append(p)
            elif _ROOT_KEY.search(text):
                out.append(p)
    return out


def _default_label(roots: list[Path], root: Path) -> str:
    versions = {_info_version(r) for r in roots}
    if len(versions) == 1 and None not in versions:
        return versions.pop()
    root = root.resolve()  # "." has no name of its own
    return (root.name if root.is_dir() else root.stem) or "spec"


def _info_version(root: Path) -> str | None:
    import yaml
    try:
        with open(root, encoding="utf-8") as f:
            data = yaml.load(f, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
        version = str(data["info"]["version"]).strip()
        if version and ":" not in version and "/" not in version:
            return version
    except Exception:
        pass
    return None


class SpecSet:
    """Several specs under labels, for example two versions of one API."""

    def __init__(self):
        self.specs: dict[str, Spec] = {}

    def add(self, spec: Spec) -> Spec:
        if spec.label in self.specs:
            raise ValueError(f"label {spec.label!r} is already loaded")
        self.specs[spec.label] = spec
        return spec

    def load(self, path, label: str | None = None) -> Spec:
        return self.add(load_spec(path, label))

    def labels(self) -> list[str]:
        return sorted(self.specs, key=label_key)

    def get(self, label: str | None = None) -> Spec:
        """Exact match only: "1.3" is not "11.3" or "1.30". No label is fine when one spec is loaded."""
        if label is None:
            if len(self.specs) == 1:
                return next(iter(self.specs.values()))
            raise KeyError(f"say which spec; loaded: {', '.join(self.labels()) or 'none'}")
        if label not in self.specs:
            raise KeyError(f"no spec labelled {label!r}; loaded: {', '.join(self.labels()) or 'none'}")
        return self.specs[label]

    def get_schema(self, name: str, label: str | None = None, expand: int = 0,
                   max_chars: int | None = None) -> query.SchemaResult:
        return self.get(label).get_schema(name, expand, max_chars)

    def get_operation(self, method: str, path: str | None = None, label: str | None = None, expand: int = 0,
                      errors: bool = False, max_chars: int | None = None) -> query.OperationResult:
        return self.get(label).get_operation(method, path, expand, errors, max_chars)

    def find_field(self, name: str, label: str | None = None) -> query.FieldResult:
        return self.get(label).find_field(name)

    def list_operations(self, label: str | None = None, filter: str | None = None) -> query.OperationList:
        return self.get(label).list_operations(filter)

    def search(self, text: str, label: str | None = None, limit: int = 10) -> query.SearchResult:
        return self.get(label).search(text, limit=limit)

    def source(self, node_id: str) -> query.SourceResult:
        """The label is part of the id."""
        return query.source(self.get(node_id.split(":", 1)[0]), node_id)

    def names_in(self, text: str, label: str | None = None) -> query.Mentions:
        return self.get(label).names_in(text)

    def compare(self, name: str, a: str, b: str) -> _compare.Comparison:
        """What changed in a schema, operation or field from the spec labelled `a` to the one labelled `b`."""
        return _compare.compare(self.get(a), self.get(b), name)

    def diff(self, a: str, b: str) -> _compare.SpecDiff:
        """Everything that changed from the spec labelled `a` to the one labelled `b`."""
        return _compare.diff(self.get(a), self.get(b))
