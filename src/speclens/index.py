"""Indexes over one graph. One walk over the nodes; each visitor builds one index.

- NameIndex: a name -> node ids, exactly, ignoring case, or by plain words ("pet owners" -> PetOwner).
- FieldIndex: a field name -> every place it is written.
- KeywordIndex: BM25F over names, summaries and descriptions; plain BM25 for a query that reads like a description.
- UsedBy: a node -> the operations that reach it, directly or nested. Computed on request and remembered,
  since most nodes are never asked about.
"""

import difflib
import math
import re
from collections import Counter, defaultdict, deque
from typing import ClassVar

from . import graph as g
from .graph import Graph, Node
from .text import key, stem, terms, variants, words

SEARCHED = (g.OPERATION, g.SCHEMA, g.FIELD, g.PARAMETER)


class NameIndex:
    def __init__(self):
        self.exact: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        self.lower: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        self.plain: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        self.member_names: set[str] = set()  # lowercased field and parameter names

    def visit(self, node: Node, graph: Graph):
        names = [node.name]
        if node.kind == g.OPERATION and node.data.get("operationId"):
            names.append(str(node.data["operationId"]))
        if node.kind in (g.FIELD, g.PARAMETER) and node.data.get("name"):
            self.member_names.add(str(node.data["name"]).lower())
        for name in names:
            self.exact[node.kind][name].append(node.id)
            self.lower[node.kind][name.lower()].append(node.id)
            if node.kind != g.SCHEMA or node.data.get("named"):
                self.plain[node.kind][key(name)].append(node.id)

    def finish(self, graph: Graph):
        pass

    def lookup(self, kind: str, name: str) -> tuple[list[str], str]:
        """(ids, how): the first of exact, ignoring case, plain words that finds anything."""
        for how, table, k in (("exact", self.exact, name), ("ignoring case", self.lower, name.lower())):
            ids = table[kind].get(k)
            if ids:
                return _unique(ids), how
        ids = [i for v in sorted(variants(key(name))) for i in self.plain[kind].get(v, ())]
        return _unique(ids), "plain words" if ids else "none"

    def suggest(self, kind: str, name: str, limit: int = 5) -> list[str]:
        names = list(self.exact[kind])
        close = difflib.get_close_matches(name, names, n=limit, cutoff=0.6)
        low = name.lower()
        contains = [n for n in names if low and low in n.lower() and n not in close]
        return (close + sorted(contains, key=len))[:limit]


class FieldIndex:
    def __init__(self):
        self.by_name: dict[str, list[str]] = defaultdict(list)
        self.by_lower: dict[str, list[str]] = defaultdict(list)
        self.plain: dict[str, list[str]] = defaultdict(list)  # names of two words or more, by key

    def visit(self, node: Node, graph: Graph):
        if node.kind == g.FIELD:
            self.by_name[node.data["name"]].append(node.id)
            self.by_lower[node.data["name"].lower()].append(node.id)
            if len(words(node.data["name"])) > 1:
                self.plain[key(node.data["name"])].append(node.id)

    def finish(self, graph: Graph):
        pass

    def lookup(self, name: str) -> tuple[list[str], str]:
        if name in self.by_name:
            return list(self.by_name[name]), "exact"
        ids = self.by_lower.get(name.lower())
        if ids:
            return list(ids), "ignoring case"
        ids = [i for n, found in self.by_name.items() if key(n) in variants(key(name)) for i in found]
        return ids, "plain words" if ids else "none"

    def suggest(self, name: str, limit: int = 5) -> list[str]:
        return difflib.get_close_matches(name, list(self.by_name), n=limit, cutoff=0.6)


_PATH = re.compile(r"(?:\b(get|put|post|delete|patch|head|options|trace)\s+)?(?<![\w}])(/[\w{}\-.~/]*)",
                   re.IGNORECASE)  # a path written out, with or without its method
# "get widget by id", "delete a widget by its ID", "update widget by widgetId": one item of a collection
_BY_ID = re.compile(r"\b(?:by|with|using)\s+(?:its\s+|the\s+|an?\s+|their\s+)?"
                    r"((?i:id|ids|identifier|uuid)|[A-Za-z][A-Za-z0-9]*(?:Id|ID|_id))\b")
_VERB_METHODS = {**dict.fromkeys(("get", "fetch", "retrieve", "read", "find", "show", "lookup", "describe", "view"),
                                 ("GET",)),
                 **dict.fromkeys(("update", "modify", "edit", "replace", "change", "patch", "put"), ("PUT", "PATCH")),
                 **dict.fromkeys(("delete", "remove"), ("DELETE",))}


class KeywordIndex:
    """BM25F: BM25 over a node's fields kept apart, each with its own weight and length normalisation, so a long
    description does not bury a name. The fields: name (a schema's or parameter's, a field's own, an operation's
    path without its parameters), summary (summary or title), description, and other (operationId, tags). A node
    whose whole name is the query scores more. A last field, context, holds what a node belongs to: a field's
    schema, a parameter's operation, and a schema's own name, since a schema is the context of its fields; so
    "what does id mean on an owner" finds Owner.id, while "what fields does an owner have" still finds Owner. An
    inline schema has no name (where it is written is not one). A path written in the query puts the operations
    with exactly that path first; "get widget by id" puts first the operations on one widget (see _items_by_id).

    A query that reads like a description (one node's description holds most of its terms) is ranked by plain
    BM25 over all of a node's text instead: for a sentence copied or paraphrased from a description, weighing
    names more only gets in the way. Weights and the routing were set on half the public specs in the test sample
    (split by vendor) and checked on the other half,
    where every kind of question ranks as well as plain BM25 or better, to within 0.01."""

    FIELDS = ("name", "summary", "description", "other", "context")
    WEIGHT: ClassVar[dict[str, float]] = {"name": 8.0, "summary": 5.0, "description": 1.0, "other": 3.0, "context": 2.0}
    B: ClassVar[dict[str, float]] = {"name": 0.75, "summary": 1.0, "description": 0.3, "other": 0.75, "context": 0.5}
    K1 = 3.0
    EXACT = 1.5  # a whole name equal to the query
    # Plain BM25 over all of a node's text, for a query that reads like a description.
    PLAIN_K1, PLAIN_B = 1.5, 0.75
    LIKE_DESCRIPTION = 0.75  # this share of the query's terms in one node's description
    LIKE_DESCRIPTION_TERMS = 3  # and at least this many terms

    def __init__(self):
        self.docs: dict[str, dict[str, Counter]] = {}
        self.lengths: dict[str, dict[str, int]] = {}
        self.names: dict[str, str] = {}  # node id -> the key of its whole name
        self.paths: dict[str, list[tuple[str, str]]] = defaultdict(list)  # path -> (method, operation id)
        # the words of the collection an operation acts on one item of: /widgets/{widgetId} -> ("widget",)
        self.items: dict[tuple[str, ...], list[tuple[str, str, str]]] = defaultdict(list)  # (method, id, parameter)
        self.postings: dict[str, list[str]] = defaultdict(list)
        self.plain: dict[str, Counter] = {}  # node id -> all its text in one bag, for plain BM25
        self.plain_lengths: dict[str, int] = {}
        self.plain_postings: dict[str, list[str]] = defaultdict(list)

    def visit(self, node: Node, graph: Graph):
        if node.kind not in SEARCHED:
            return
        if node.kind == g.SCHEMA and not node.data.get("named") and not node.data.get("description"):
            return  # an inline schema with nothing to say is found through its field
        d = node.data
        context = ""
        if node.kind == g.SCHEMA and not d.get("named"):
            name = ""  # where an inline schema is written is not its name; its description says what it is
        elif node.kind == g.OPERATION:  # what it is called: the fixed parts of its path, not its parameters
            name = re.sub(r"\{[^}]*\}", " ", str(d.get("path", "")))
        elif node.kind in (g.FIELD, g.PARAMETER):
            name = str(d.get("name", ""))  # a field's own name, not its schema's
            if node.kind == g.FIELD:  # Owner.id -> Owner
                context = node.name.rsplit(".", 1)[0] if "." in node.name else ""
            else:  # GET /owners/{ownerId}#path/ownerId -> GET /owners/{ownerId}
                context = node.name.split("#", 1)[0] if "#" in node.name else ""
        else:
            name = context = node.name
        texts = {"name": name, "summary": " ".join(str(x) for x in (d.get("summary"), d.get("title")) if x),
                 "description": str(d.get("description") or ""),
                 "other": " ".join([str(d.get("operationId") or "")] + [str(x) for x in d.get("tags") or []]),
                 "context": context}
        fields = {f: Counter(terms(texts[f])) for f in self.FIELDS}
        if not any(fields.values()):
            return
        self.docs[node.id] = fields
        self.lengths[node.id] = {f: sum(c.values()) for f, c in fields.items()}
        self.names[node.id] = key(name)
        if node.kind == g.OPERATION:
            self.paths[str(d.get("path", ""))].append((str(d.get("method", "")).upper(), node.id))
            parts = [x for x in str(d.get("path", "")).split("/") if x]
            if len(parts) >= 2 and re.fullmatch(r"\{[^}]+\}", parts[-1]) and "{" not in parts[-2]:
                item = tuple(stem(w) for w in words(parts[-2]))
                if item:
                    self.items[item].append((str(d.get("method", "")).upper(), node.id, key(parts[-1][1:-1])))
        for t in set().union(*fields.values()):
            self.postings[t].append(node.id)
        # Plain BM25's bag: what names the node counts twice (a field's own name, an operation's path and summary).
        if node.kind == g.OPERATION:
            names = [node.name, node.name, d.get("summary"), d.get("summary")]
        else:
            names = [d.get("name", ""), d.get("name", "") if node.kind == g.FIELD else node.name, d.get("summary")]
        text = names + [d.get("title"), d.get("description"), d.get("operationId"), " ".join(map(str, d.get("tags") or []))]
        bag = Counter(t for part in text if part for t in terms(str(part)))
        if bag:
            self.plain[node.id] = bag
            self.plain_lengths[node.id] = sum(bag.values())
            for t in bag:
                self.plain_postings[t].append(node.id)

    def finish(self, graph: Graph):
        self.average = {}
        for f in self.FIELDS:
            sizes = [n[f] for n in self.lengths.values() if n[f]]
            self.average[f] = sum(sizes) / len(sizes) if sizes else 1.0
        self.plain_average = sum(self.plain_lengths.values()) / len(self.plain_lengths) if self.plain_lengths else 1.0

    def search(self, query: str, kinds=None, limit: int = 10) -> list[tuple[str, float]]:
        scores = self._plain(query) if self.reads_like_description(query) else self._fielded(query)
        if not _PATH.search(query):  # "get widget by id": that item's operations first, the exact name before others
            top = max(scores.values(), default=0.0)
            for node_id, exact in self._items_by_id(query):
                if node_id in scores:
                    scores[node_id] += top * (2 if exact else 1)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        if kinds:
            ranked = [(i, s) for i, s in ranked if i.split(":", 1)[1].split("/", 1)[0] in kinds]
        return ranked[:limit]

    def reads_like_description(self, query: str) -> bool:
        """A query most of whose terms one node's description holds: plain BM25 ranks those best, BM25F the rest."""
        wanted = set(terms(query))
        if len(wanted) < self.LIKE_DESCRIPTION_TERMS:
            return False
        held: Counter = Counter()
        for t in wanted:
            for node_id in self.postings.get(t, ()):
                if self.docs[node_id]["description"][t]:
                    held[node_id] += 1
        return bool(held) and max(held.values()) >= self.LIKE_DESCRIPTION * len(wanted)

    def _plain(self, query: str) -> dict[str, float]:
        n, k1, b = len(self.plain), self.PLAIN_K1, self.PLAIN_B
        scores: dict[str, float] = defaultdict(float)
        for t in set(terms(query)):
            ids = self.plain_postings.get(t, ())
            if not ids:
                continue
            idf = math.log(1 + (n - len(ids) + 0.5) / (len(ids) + 0.5))
            for node_id in ids:
                tf = self.plain[node_id][t]
                size = self.plain_lengths[node_id] / self.plain_average
                scores[node_id] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * size))
        return scores

    def _fielded(self, query: str) -> dict[str, float]:
        n = len(self.docs)
        scores: dict[str, float] = defaultdict(float)
        for t in set(terms(query)):
            ids = self.postings.get(t, ())
            if not ids:
                continue
            idf = math.log(1 + (n - len(ids) + 0.5) / (len(ids) + 0.5))
            for node_id in ids:
                doc, size = self.docs[node_id], self.lengths[node_id]
                tf = sum(self.WEIGHT[f] * doc[f][t] / (1 - self.B[f] + self.B[f] * size[f] / self.average[f])
                         for f in self.FIELDS if doc[f][t])
                scores[node_id] += idf * tf / (self.K1 + tf)
        whole = key(query)
        for node_id in scores:
            if whole and self.names[node_id] == whole:
                scores[node_id] *= self.EXACT
        # A path written out names its operations exactly: they come first, in their own order.
        top = max(scores.values(), default=0.0)
        written = _PATH.findall(query)
        for method, path in written:
            path = path.rstrip("./?") or "/"
            for m, node_id in self.paths.get(path, ()):
                if node_id in scores and (not method or method.upper() == m):
                    scores[node_id] += top
        return scores

    def _items_by_id(self, query: str) -> list[tuple[str, bool]]:
        """"get widget by id": the operations on one item of a collection named by the words before "by", whose
        path ends in that item's parameter (GET /widgets/{widgetId}, not /widgets/{widgetId}/images/{imageId}),
        with the method the verb says; and whether the words name the collection whole (/widgets/{widgetId}) or
        only its last words (/userWidgets/{widgetId}). Only for a verb it knows ("reopen a matter by id" acts on
        the item, but not by getting, changing or deleting it), and an id that is the item's own ("widget by
        widgetId"): "reports by groupId" asks for something under a group, not for an item."""
        by = _BY_ID.search(query)
        if not by:
            return []
        before = [w.lower() for w in words(query[:by.start()])]
        verbs = [v for v in (w if w in _VERB_METHODS else stem(w) for w in before) if v in _VERB_METHODS]
        if not verbs:
            return []
        methods = _VERB_METHODS[verbs[-1]]
        said = tuple(stem(w) for w in before)
        if by.group(1).lower() not in ("id", "ids", "identifier", "uuid") and \
                key(by.group(1)) not in {key(" ".join(before[-n:]) + " id") for n in range(1, len(before) + 1)}:
            return []
        out = []
        for item, operations in self.items.items():
            exact = said[-len(item):] == item
            if exact or (said and item[-1] == said[-1]):
                out += [(node_id, exact) for m, node_id, _ in operations if m in methods]
        return out


class UsedBy:
    """Which operations reach a node, and how: the operation's own edge (a response, the request body, a
    parameter), and whether that edge points at the node itself (direct) or at something that contains it."""

    def __init__(self, graph: Graph):
        self.graph = graph
        self.cache: dict[str, list[dict]] = {}

    def __call__(self, node_id: str) -> list[dict]:
        if node_id not in self.cache:
            self.cache[node_id] = self._walk(node_id)
        return self.cache[node_id]

    def _walk(self, start: str) -> list[dict]:
        out, seen, queue = [], {start}, deque([start])
        while queue:
            current = queue.popleft()
            for e in self.graph.incoming(current):
                source = self.graph.nodes[e.source]
                if source.kind == g.OPERATION:
                    out.append({"operation": source.id, "edge": e.kind, "direct": current == start,
                                "through": current, **{k: v for k, v in e.data.items() if k != "ref"}})
                elif e.source not in seen:
                    seen.add(e.source)
                    queue.append(e.source)
        out.sort(key=lambda u: (u["operation"], u["edge"], str(u.get("status", "")), not u["direct"]))
        return out


class Indexes:
    def __init__(self, graph: Graph):
        self.names = NameIndex()
        self.fields = FieldIndex()
        self.keywords = KeywordIndex()
        visitors = (self.names, self.fields, self.keywords)
        for node in graph.nodes.values():
            for visitor in visitors:
                visitor.visit(node, graph)
        for visitor in visitors:
            visitor.finish(graph)
        self.used_by = UsedBy(graph)


def _unique(ids):
    return list(dict.fromkeys(ids))
