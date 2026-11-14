"""Check an LLM's answer against the evidence it was given.

    verify(answer, evidence, known=None, cover=None) -> Verification

`evidence` is what the model saw, as a list of any of:
- speclens results (get_schema, get_operation, find_field, compare, ...), or their `to_dict()`;
- text, such as chunks a search returned;
- (label, text) pairs: text known to come from the spec with that label, such as a chunk of version 1.2.

Findings, each with the words of the answer it is about:
- unknown-citation: a node id the answer cites that is not in the evidence;
- unknown-name: a name written as code (backticked, or camelCase, PascalCase, snake_case) found nowhere in the
  evidence;
- contradicted-claim: a claim that something is missing from, added in or removed in a version, where the
  evidence says otherwise (it shows the thing in that version, or already in an earlier one); that a field or
  parameter is required or optional, or has a limit, or has none, where the evidence shows otherwise;
- unsupported-claim: such a claim, or one that something changed between versions, that nothing in the
  evidence supports;
- wrong-count: a number of fields, parameters or options stated for a schema or operation whose complete
  list is in the evidence, matching none of the counts it shows ("12 fields" may be all of them or only
  its own; "3 inherited" must be the inherited ones);
- omitted-change: with `cover`, a breaking change in those comparisons or diffs that the answer never
  mentions.

`known` is text the answer may repeat without it being evidence, such as earlier turns of a conversation: its
names are not unknown-name findings, but they never support a citation, a count or a claim.

It reports and never rewrites: what to do with a finding is the caller's choice. Claims are found by plain
patterns ("missing in", "added in", "no longer", "removed", "changed", ...) in a sentence that names a label
from the evidence; a claim worded another way is not checked.
"""

import re
from dataclasses import asdict, dataclass, field, is_dataclass

from .query import Result
from .text import PATH, key, label_key, trim_path, words

KINDS = ("operation", "schema", "field", "parameter")
_ID = re.compile(r"(?<![\w.:-])([\w.-]+):(operation|schema|field|parameter)/")
_CODE = re.compile(r"`([^`\n]+)`")
_PART = r"[$@]?[A-Za-z_][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)*"  # pet_owner, petOwner, pet-owner, $id, @type
_IDENT = re.compile(rf"{_PART}(?:\.{_PART})*")
# An operation written as method and path: "DELETE /pets/{id}". Its path is one thing, not the names in it.
_OPERATION = re.compile(rf"\b(GET|PUT|POST|DELETE|OPTIONS|HEAD|PATCH|TRACE)\s+({PATH})")
# A kind written before a name, as in an id: "schema:Pet", "(operation: GET /pets)". The kind is not a name.
_KIND_PREFIX = re.compile(r"\b(?:operation|schema|field|parameter)\s*:(?!/)", re.I)


def _clauses(sentence: str) -> list[str]:
    """The clauses of a sentence, not splitting a path at its commas: GET /vets(name={name},city={city})."""
    keep = lambda m: m.group(0).replace(",", "\0")  # noqa: E731
    return [c.replace("\0", ",") for c in _CLAUSE.split(_OPERATION.sub(keep, sentence))]


def _operation(method: str, path: str) -> str:
    """An operation as one name: the method and the path, path templates by place ({id} and {petId} agree)."""
    return f"{method.upper()} {re.sub(r'{[^}/]*}', '{}', trim_path(path).rstrip('/') or '/')}"
# Outside backticks, what is checked as a name: camelCase starting with two lowercase letters (petId), or
# snake_case. Not PascalCase or "iOS"-like words: in prose those are mostly products and brands (JavaScript,
# GitHub, PostgreSQL), and a schema named that way is checked when it is backticked or cited.
_CODE_LIKE = re.compile(r"^[a-z]{2,}[a-z0-9]*[A-Z]|[a-z0-9]_[a-z0-9]")
_URL = re.compile(r"\b(?:https?|wss?)://\S+|\b[\w.+-]+@[\w-]+\.[\w.]+")
_WORDS = re.compile(r"[A-Za-z0-9_]+")
# Values that are part of how JSON and HTTP are written, not names a spec defines.
_COMMON = {"true", "false", "null", "GET", "PUT", "POST", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE",
           "JSON", "YAML", "HTTP", "HTTPS", "URL", "URI", "API", "UUID", "ISO", "UTF", "OpenAPI", "allOf", "oneOf",
           "anyOf", "enum", "type", "string", "integer", "number", "boolean", "array", "object", "required",
           "nullable", "readOnly", "writeOnly", "deprecated", "format", "default", "properties", "items",
           "description", "example", "examples", "discriminator", "schema", "schemas", "components", "paths",
           "parameters", "responses", "requestBody", "content", "headers", "operationId", "OAuth", "OAuth2",
           # JSON Schema keywords: the language of every spec, not a name in one
           "maxLength", "minLength", "maximum", "minimum", "exclusiveMaximum", "exclusiveMinimum", "multipleOf",
           "maxItems", "minItems", "uniqueItems", "maxProperties", "minProperties", "additionalProperties",
           "patternProperties", "pattern", "const", "not", "contains", "maxContains", "minContains",
           "dependentRequired", "dependentSchemas", "unevaluatedProperties", "unevaluatedItems", "prefixItems",
           "propertyNames", "contentMediaType", "contentEncoding", "$ref", "$defs", "$id", "$schema", "$anchor",
           "definitions", "externalDocs", "servers", "securitySchemes", "callbacks", "webhooks", "allowEmptyValue",
           "explode", "style", "xml",
           # standard HTTP header names: general knowledge, not something one spec defines
           "Accept", "Accept-Encoding", "Accept-Language", "Authorization", "Cache-Control", "Content-Encoding",
           "Content-Length", "Content-Type", "Cookie", "ETag", "Host", "If-Match", "If-None-Match",
           "If-Modified-Since", "Last-Modified", "Link", "Location", "Origin", "Retry-After", "Set-Cookie",
           "User-Agent", "WWW-Authenticate", "X-Request-Id", "X-RateLimit-Limit", "X-RateLimit-Remaining",
           # product names shaped like field names (userID is a field; macOS is not)
           "macOS", "watchOS", "tvOS", "iPadOS", "visionOS"}

_MISSING = re.compile(r"\b(missing|absent|lack(?:s|ed|ing)?|(?:has|have|had) no|there (?:is|are|was|were) no|not (?:present|included|available|defined|listed|there)|"
                      r"(?:does|do|did) ?(?:not|n't) (?:have|include|contain|exist|support|define|list)|"
                      r"(?:is|are|was|were) ?(?:not|n't) (?:in|part of|present|included|defined|there|"
                      r"available|supported|listed|offered)|"
                      r"no (?:such|longer has)|without)\b", re.I)
_ADDED = re.compile(r"\b(added|new in|newly|introduced|appears? (?:first|only) in|first appear(?:s|ed)?|"
                    r"gain(?:s|ed)?|since|starting (?:in|with|from)|as of|now (?:has|includes|contains))\b", re.I)
_REMOVED = re.compile(r"\b(removed|dropped|deleted|no longer|(?:is|are|was|were) gone|gone from|anymore|"
                      r"no more)\b", re.I)
_CHANGED = re.compile(r"\b(changed|renamed|deprecated|retyped|became|now (?:required|optional|nullable)|"
                      r"differs?|different)\b", re.I)
# Negations that say the thing before them is what is absent ("`tag` is missing"), against those that say it has
# nothing of something ("`tag` has no default", "Pet lacks `tag`": then the thing after is what is absent).
_ABSENT_BEFORE = re.compile(r"\b(?:missing|absent|not (?:present|included|available|defined|listed|there)|"
                            r"(?:is|are|was|were) ?(?:not|n't) (?:in|part of|present|included|defined|there|"
                            r"available|supported|listed|offered)|removed|dropped|deleted|gone|no longer (?:in|"
                            r"part of|present|included|defined|there|available|supported|listed|offered)|"
                            r"(?:does|do|did) ?(?:not|n't) exist)\b", re.I)
# Words that may stand between a negation and the absent name: "there is no optional `tag` field".
_BETWEEN = {"a", "an", "the", "any", "such", "field", "fields", "property", "properties", "parameter",
            "parameters", "attribute", "attributes", "option", "named", "called", "optional", "required",
            "nullable", "deprecated", "separate", "dedicated", "explicit", "or", "and", "nor", "longer", "more",
            "has", "have", "had", "include", "includes", "included", "contain", "contains", "list", "lists",
            "define", "defines", "support", "supports", "offer", "offers", "accept", "accepts", "return", "returns"}
# Words that can follow a name a claim is about: "no `tag` field in 1.2", "`tag` is missing", "`tag` anymore".
# Any other word after it is what the claim is about: "no encryption guidance" (not "no encryption").
_AFTER_A_NAME = {"field", "property", "parameter", "attribute", "header", "option", "value", "key", "element",
                 "member", "entry", "column", "schema", "object", "type", "operation", "endpoint", "is", "are",
                 "was", "were", "has", "have", "had", "does", "do", "did", "in", "from", "for", "on", "at", "of",
                 "to", "or", "and", "nor", "but", "anymore", "any", "either", "itself", "there", "here", "yet",
                 "now", "since", "until", "before", "after", "as", "with", "without", "which", "that", "no",
                 "not", "exists", "existed", "exist", "appears", "appeared", "isn", "aren", "wasn", "weren",
                 "doesn", "didn", "hasn", "haven", "remains", "remained", "still", "too", "either", "also",
                 "anywhere", "altogether", "entirely", "completely", "at", "between", "within", "under", "inside",
                 "missing", "absent", "removed", "dropped", "deleted", "gone", "listed", "defined", "included",
                 "present", "available", "supported", "offered"}
# What follows these names the new thing, which is there: "Foo was removed and replaced by Bar".
_SUCCESSOR = re.compile(r"\b(?:replaced (?:by|with)|renamed (?:to|as)|superseded by|succeeded by|in favou?r of|"
                        r"moved (?:to|into)|merged into|now called|instead of which)\b", re.I)
# Words that can stand before "field", "property", ... without being a name: "the schema and field descriptions".
_NOT_A_NAME = {"the", "a", "an", "this", "that", "these", "those", "new", "same", "and", "or", "nor", "of", "each",
               "every", "any", "no", "which", "what", "its", "their", "one", "other", "another", "such", "some",
               "all", "both", "either", "neither", "given", "custom", "extra", "additional", "separate", "single",
               "optional", "required", "nested", "top", "level", "data", "schema", "request", "response", "query",
               "path", "header", "body", "object", "array", "string", "per", "by", "to", "in", "on", "with"}
# Words before "field" that say which kind of field, not its name: "the removed field", "a date-like field".
_NOT_A_FIELD_NAME = _NOT_A_NAME | {"old", "first", "last", "previous", "former", "existing", "following", "only",
                                   "main", "primary", "similar", "related", "corresponding", "relevant", "specific",
                                   "certain", "more", "most", "many", "several", "few", "whole", "entire", "full",
                                   "deprecated", "removed", "added", "renamed", "changed", "missing", "unknown",
                                   "is", "was", "has", "have", "had", "be", "been", "not", "also", "then", "now",
                                   "still", "as", "for", "from", "at", "inherited", "own", "mandatory"}
# A name in text that is not prose: in backticks, or a YAML or JSON key.
_KEYED = re.compile(r"`([^`\n]+)`|^[ \t-]*[\"']?([$@]?[A-Za-z_][\w.$@-]*)[\"']?[ \t]*:(?![/\w])", re.M)
# English words that are never what a claim is about when written as plain words, even if the evidence's prose
# has them: "these fields are not present in 1.2", "1.2 has no such endpoint".
_FUNCTION = {"a", "an", "the", "this", "that", "these", "those", "it", "its", "they", "them", "their", "there",
             "here", "is", "are", "was", "were", "be", "been", "being", "has", "have", "had", "do", "does", "did",
             "not", "no", "nor", "and", "or", "but", "if", "then", "than", "so", "such", "as", "at", "by", "for",
             "from", "in", "into", "of", "on", "onto", "to", "with", "without", "within", "via", "per", "any",
             "all", "some", "each", "every", "both", "either", "neither", "other", "another", "same", "which",
             "what", "who", "whom", "whose", "where", "when", "why", "how", "also", "only", "just", "still",
             "yet", "now", "longer", "more", "most", "less", "least", "very", "can", "could", "may", "might",
             "must", "shall", "should", "will", "would", "present", "included", "available", "defined", "listed",
             "missing", "absent", "added", "removed", "new", "version", "versions", "spec", "field", "fields"}
# A reference in brackets, as an answer cites a source: [3], [_chunk_pets_0], [doc-2]. What it names is where a
# statement comes from, never what the statement is about.
_REFERENCE = re.compile(r"\[[^\]\s`]+\]")
_CLAUSE = re.compile(r",|;|\bbut\b|\bwhile\b|\bwhereas\b|\bhowever\b|\balthough\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass
class Finding:
    kind: str  # unknown-citation, unknown-name, contradicted-claim, unsupported-claim
    text: str  # the words of the answer it is about
    detail: str
    evidence: list[str] = field(default_factory=list)  # node ids or labels behind a contradiction
    places: list[str] = field(default_factory=list)  # omitted-change: every schema or operation it was reached from

    def line(self) -> str:
        return f"{self.kind}: {self.text!r} — {self.detail}"


@dataclass
class Verification:
    findings: list[Finding]
    checked_citations: int
    checked_names: int
    checked_claims: int
    checked_counts: int = 0
    checked_changes: int = 0  # breaking changes the answer was to cover
    checked_required: int = 0  # claims that something is required or optional
    checked_limits: int = 0  # claims of a limit: maximum length, minimum, most items

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict:
        return {**asdict(self), "ok": self.ok}

    def to_text(self) -> str:
        head = (f"checked {self.checked_citations} citations, {self.checked_names} names, "
                f"{self.checked_claims} version claims, {self.checked_counts} counts"
                + (f", {self.checked_required} required/optional" if self.checked_required else "")
                + (f", {self.checked_limits} limits" if self.checked_limits else "")
                + (f", {self.checked_changes} breaking changes" if self.checked_changes else "") + ": ")
        if not self.findings:
            return head + "no findings"
        n = len(self.findings)
        return "\n".join([head + f"{n} finding{'s' * (n != 1)}"] + [f"  {f.line()}" for f in self.findings])

    def __str__(self):
        return self.to_text()


class _Evidence:
    """What the evidence holds: node ids, the words in it, the labels, what is known in each label, and the
    comparisons and full field lists that can support a claim about versions."""

    def __init__(self, items):
        self.ids: set[str] = set()
        self.words: set[str] = set()
        self.labels: set[str] = set()
        self.known_in: dict[str, set[str]] = {}  # label -> names shown to exist there
        self.comparisons: list[dict] = []
        self.field_lists: dict[tuple[str, str], set[str]] = {}  # (label, schema) -> its complete fields
        self.members: dict[str, set[tuple[str, str]]] = {}  # label -> (schema, field) shown to exist there
        self.schemas: set[str] = set()  # schema names in the evidence
        self.names: set[str] = set()  # names of things, not prose: from ids, results, keys and code in text
        # name -> label -> whether shown required; and (schema, field) -> label -> the same
        self.required: dict[str, dict[str, set[bool]]] = {}
        self.required_in: dict[tuple[str, str], dict[str, set[bool]]] = {}
        # name -> label -> keyword -> values shown (maxLength 3); and (schema, field) -> the same
        self.limits: dict[str, dict[str, dict[str, set[float]]]] = {}
        self.limits_in: dict[tuple[str, str], dict[str, dict[str, set[float]]]] = {}
        self.counts: list[_Countable] = []  # schemas and operations whose complete lists the evidence holds
        self.operations: dict[str, set[str]] = {}  # label -> operations shown to exist there
        self.methods: dict[tuple[str, str], set[str]] = {}  # (label, path) -> all the methods it has
        self.types: dict[str, set[str]] = {}  # field or parameter name -> the types its rows show
        self._anywhere: set[str] | None = None
        for item in items:
            self.add(item)

    def add(self, item, label=None):
        self._anywhere = None
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str):
            label, item = item
            self.labels.add(label)
        if isinstance(item, (list, tuple, set)):  # several results together
            for x in item:
                self.add(x, label)
        elif is_dataclass(item) and hasattr(item, "to_dict"):
            self.add_text(item.to_text(full=True) if isinstance(item, Result) else item.to_text(), label)
            self.add_dict(item.to_dict())
        elif is_dataclass(item) and not isinstance(item, type):
            self.add(asdict(item), label)
        elif isinstance(item, dict):
            self.add_dict(item)
            self.add_text(" ".join(_strings(item)), label)
        else:
            self.add_text(str(item), label)

    def add_id(self, node_id: str):
        self.ids.add(node_id)
        label, rest = node_id.split(":", 1)
        kind, name = rest.split("/", 1)
        self.labels.add(label)
        self.words.update(_words_of(name))
        self.names.update(_words_of(name))
        self.known_in.setdefault(label, set()).update(_words_of(name))
        if kind == "schema":
            self.schemas.add(name)
        elif kind == "operation" and " " in name:
            self.operations.setdefault(label, set()).add(_operation(*name.split(" ", 1)))
        elif kind == "field" and "." in name:
            owner, member = name.rsplit(".", 1)
            self.schemas.add(owner)
            self.members.setdefault(label, set()).add((owner, member))

    def mark_required(self, label: str, name: str, required: bool, owner: str | None = None):
        self.required.setdefault(name, {}).setdefault(label, set()).add(required)
        if owner:
            self.required_in.setdefault((owner, name), {}).setdefault(label, set()).add(required)

    def add_required(self, d: dict):
        """Whether each field and parameter in a result is required, in the version its id names."""
        owner = d.get("name") if isinstance(d.get("fields"), list) else None
        stack = [(d, None)]
        while stack:
            v, holder = stack.pop()
            if isinstance(v, dict):
                if isinstance(v.get("name"), str) and isinstance(v.get("required"), bool) and \
                        _ID.match(str(v.get("id", ""))):
                    self.mark_required(str(v["id"]).split(":", 1)[0], v["name"], v["required"], holder)
                for k, x in v.items():
                    stack.append((x, owner if k == "fields" and v is d else holder))
            elif isinstance(v, (list, tuple)):
                stack.extend((x, holder) for x in v)

    def mark_limit(self, label: str, name: str, keyword: str, value, owner: str | None = None):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return
        self.limits.setdefault(name, {}).setdefault(label, {}).setdefault(keyword, set()).add(float(value))
        if owner:
            self.limits_in.setdefault((owner, name), {}).setdefault(label, {}).setdefault(keyword, set()).add(
                float(value))

    def add_types(self, d: dict):
        """The type each field and parameter row shows: `Status (string enum [...])`, `array of Status`."""
        stack = [d]
        while stack:
            v = stack.pop()
            if isinstance(v, dict):
                if isinstance(v.get("name"), str) and isinstance(v.get("type"), str) and \
                        _ID.match(str(v.get("id", ""))):
                    self.types.setdefault(v["name"], set()).add(v["type"])
                stack.extend(v.values())
            elif isinstance(v, (list, tuple)):
                stack.extend(v)

    def add_limits(self, d: dict):
        """The limits each field and parameter row shows, in the version its id names."""
        stack = [d]
        while stack:
            v = stack.pop()
            if isinstance(v, dict):
                node_id = str(v.get("id", ""))
                if isinstance(v.get("name"), str) and isinstance(v.get("limits"), dict) and _ID.match(node_id):
                    label, rest = node_id.split(":", 1)
                    kind, name = rest.split("/", 1)
                    owner = name.rsplit(".", 1)[0] if kind == "field" and "." in name else None
                    for k, x in v["limits"].items():
                        self.mark_limit(label, v["name"], k, x, owner)
                stack.extend(v.values())
            elif isinstance(v, (list, tuple)):
                stack.extend(v)

    def limit_changes(self, labels: dict, changes: list):
        """What comparisons show: a limit before and after ("maxLength 1000" -> "maxLength 50")."""
        for c in changes:
            if not isinstance(c, dict) or c.get("kind") not in ("constraint-tightened", "constraint-loosened"):
                continue
            for side, label in (("before", labels["a_label"]), ("after", labels["b_label"])):
                m = re.fullmatch(r"(\w+) (-?[\d.]+(?:e[+-]?\d+)?)", str(c.get(side) or ""))
                if m:
                    for name in _changed_thing(c):
                        self.mark_limit(label, name, m.group(1), float(m.group(2)))

    def required_changes(self, labels: dict, changes: list):
        """What comparisons show: a parameter or field added as optional or required, required added or removed."""
        for c in changes:
            if not isinstance(c, dict):
                continue
            kind, names = c.get("kind"), _changed_thing(c)
            now = {"parameter-added": False, "required-parameter-added": True, "field-added": False,
                   "required-field-added": True, "required-added": True, "required-removed": False}.get(kind)
            if now is None:
                continue
            for name in names:
                self.mark_required(labels["b_label"], name, now)
                if kind in ("required-added", "required-removed"):
                    self.mark_required(labels["a_label"], name, not now)

    def present(self, label: str, subject: str, containers: list[str]):
        """Whether the evidence shows `subject` in that version: True, False (a complete field list of the
        container lacks it), or None (it does not say)."""
        op = _OPERATION.fullmatch(subject)
        if op:
            name = _operation(*op.groups())
            if name in self.operations.get(label, set()):
                return True
            methods = self.methods.get((label, name.split(" ", 1)[1]))
            return False if methods and name.split(" ", 1)[0] not in methods else None
        if containers:
            for c in containers:
                if (label, c) in self.field_lists:
                    return subject in self.field_lists[(label, c)]
                if (c, subject) in self.members.get(label, set()):
                    return True
            return None
        return True if subject in self.known_in.get(label, set()) else None

    def anywhere(self) -> set[str]:
        """Every name shown to exist in some version."""
        if self._anywhere is None:
            self._anywhere = set().union(*self.known_in.values()) if self.known_in else set()
        return self._anywhere

    def add_text(self, text: str, label: str | None):
        self.words.update(_words_of(_without_ids(text)))
        for m in _KEYED.finditer(text):  # `tag`, a YAML or JSON key: tag:, "tag":
            self.names.update(_words_of(m.group(1) or m.group(2)))
        for node_id, _ in _cited_ids(text):
            self.add_id(node_id)
        if label is not None:
            self.known_in.setdefault(label, set()).update(_words_of(text))

    def add_dict(self, d: dict):
        for s in _strings(d):
            if _ID.match(s):
                self.add_id(s)
        self.names.update(n for v in _named(d) for n in _words_of(v))
        self.add_required(d)
        self.add_limits(d)
        self.add_types(d)
        for m in d.get("mentions") or []:  # a path asked with a method it lacks: all the methods it has
            if isinstance(m, dict) and m.get("how") == "path (other method)":
                ops = [s.split(":", 1) for s in m.get("ids") or [] if _ID.match(s)]
                for label, rest in ops:
                    method, path = _operation(*rest.split("/", 1)[1].split(" ", 1)).split(" ", 1)
                    self.methods.setdefault((label, path), set()).add(method)
        if "changes" in d and "a_label" in d and "b_label" in d:
            self.comparisons.append(d)
            self.labels.update((d["a_label"], d["b_label"]))
            self.required_changes(d, d.get("changes") or [])
            self.limit_changes(d, d.get("changes") or [])
        if "schemas_changed" in d and "a_label" in d and "b_label" in d:  # a diff: one comparison per thing
            labels = {"a_label": d["a_label"], "b_label": d["b_label"]}
            self.labels.update(labels.values())
            for what in ("operation", "schema"):
                for name, changes in (d.get(f"{what}s_changed") or {}).items():
                    self.comparisons.append({"what": what, "name": name, **labels, "changes": changes})
                    self.required_changes(labels, changes)
                    self.limit_changes(labels, changes)
                for kind in ("added", "removed"):
                    for name in d.get(f"{what}s_{kind}") or []:
                        self.comparisons.append({"what": what, "name": name, **labels,
                                                 "changes": [{"kind": f"{what}-{kind}", "where": ""}]})
        if "fields" in d and "id" in d and isinstance(d["fields"], list) and _ID.match(str(d["id"])):
            lab = d["id"].split(":", 1)[0]
            names = {f["name"] for f in d["fields"]}
            if d.get("options") or d.get("conditional_fields"):
                # Options and conditions hold fields of their own: the list shows these exist, not that
                # nothing else does.
                self.members.setdefault(lab, set()).update((d["name"], n) for n in names)
            else:
                self.field_lists[(lab, d["name"])] = names
            self.schemas.add(d["name"])
        if d.get("what") == "schema" and d.get("name"):
            self.schemas.add(d["name"])
        countable = _countable(d)
        if countable and countable not in self.counts:
            self.counts.append(countable)
        for e in d.get("expanded") or []:  # schemas given with expand: results of their own
            if isinstance(e, dict):
                self.add_dict(e)


@dataclass
class _Countable:
    """What can be counted on one schema or operation result: noun -> qualifier -> number."""
    label: str
    kind: str  # "schema" or "operation"
    names: tuple[str, ...]  # what an answer may call it
    counts: dict[str, dict[str, int]]
    parents: dict[str, int]  # schema -> how many fields come from it


def _countable(d: dict) -> _Countable | None:
    node_id = str(d.get("id") or "")
    if not _ID.match(node_id):
        return None
    label = node_id.split(":", 1)[0]
    if isinstance(d.get("fields"), list) and node_id.split(":", 1)[1].startswith("schema/"):
        fields, counts, parents = d["fields"], {}, {}
        if not d.get("conditional_fields") and not d.get("options"):  # otherwise the list is not all of them
            inherited = [f for f in fields if f.get("inherited_from")]
            required = sum(1 for f in fields if f.get("required"))
            counts["fields"] = {"total": len(fields), "own": len(fields) - len(inherited),
                                "inherited": len(inherited), "required": required,
                                "optional": len(fields) - required}
            for f in inherited:
                for via in dict.fromkeys(f.get("via") or [f["inherited_from"]]):
                    name = str(via).split("/", 1)[-1]
                    parents[name] = parents.get(name, 0) + 1
        if d.get("options"):
            counts["options"] = {"total": len(d["options"])}
        return _Countable(label, "schema", (d["name"],), counts, parents) if counts else None
    if isinstance(d.get("parameters"), list) and d.get("method") and d.get("path"):
        params = d["parameters"]
        required = sum(1 for p in params if p.get("required"))
        counts = {"total": len(params), "required": required, "optional": len(params) - required}
        for where in ("path", "query", "header", "cookie"):
            counts[where] = sum(1 for p in params if p.get("location") == where)
        names = (f"{d['method']} {d['path']}",) + ((d["operation_id"],) if d.get("operation_id") else ())
        return _Countable(label, "operation", names, {"parameters": counts}, {})
    return None


def _named(value):
    """The values of `name` keys in a result: fields, parameters, schemas."""
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            if isinstance(v.get("name"), str):
                yield v["name"]
            stack.extend(v.values())
        elif isinstance(v, (list, tuple)):
            stack.extend(v)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _words_of(text: str) -> set[str]:
    """Every name-like token, and each part of a dotted one: `Pet.owner.name` gives all three and the whole."""
    out = set()
    for token in _IDENT.findall(text):
        out.add(token)
        out.update(token.split("."))
    return out


def _without_ids(text: str) -> str:
    out, last = [], 0
    for _, (start, end) in _cited_ids(text):
        out.append(text[last:start])
        last = end
    return "".join(out) + text[last:]


def _cited_ids(answer: str) -> list[tuple[str, tuple[int, int]]]:
    """(id, span) for ids written in the text: `label:kind/name`, up to a closing bracket, a file:line, or the
    line's end."""
    out = []
    for match in _ID.finditer(answer):
        rest = answer[match.end():]
        end, depth = len(rest), 0
        for i, ch in enumerate(rest):  # a name can hold brackets itself: `items[id]`
            if ch == "[":
                depth += 1
            elif ch == "]":
                if depth == 0:
                    end = i
                    break
                depth -= 1
            elif ch == "\n" or rest.startswith((" — ", " ("), i):
                end = i
                break
            elif ch in ",;" and depth == 0 and _ID.match(rest[i + 1:].lstrip()):
                end = i  # several ids in one bracket: [v1:schema/Pet, v1:operation/GET /pets]
                break
        name = rest[:end]
        where = re.search(r" [\w./-]+\.(?:ya?ml|json):\d+(?:-\d+)?$", name)
        if where:
            name = name[:where.start()]
        name = name.rstrip(" .,;:)")
        end = match.end() + len(name)
        if where:
            end = match.end() + where.end()
        out.append((f"{match.group(1)}:{match.group(2)}/{name}", (match.start(), end)))
    return out


def _names(answer: str) -> list[str]:
    """Names written as code: backticked, or shaped like an identifier (camelCase, PascalCase with two
    capitals, snake_case, letters and digits mixed)."""
    found = []
    without_ids = _URL.sub(" ", _without_ids(answer))
    for code in _CODE.findall(without_ids):
        code = code.strip()
        if code.startswith("/") or " " in code or re.fullmatch(r"[\d.\-+eE]+|'.*'|\".*\"", code) or \
                _date_format(code):
            continue  # a path, a phrase, a number, a quoted value or a date format
        found.extend(t for t in _IDENT.findall(code) if t)
    plain = _CODE.sub(" ", without_ids)
    for token in _IDENT.findall(plain):
        if any(_CODE_LIKE.search(part) for part in token.split(".")):
            found.append(token)
    return list(dict.fromkeys(found))


def _date_format(code: str) -> bool:
    """A date or time format written as a placeholder: `YYYY-MM-DD`, `HH:MM:SS`, `YYYY-MM-DDThh:mm:ssZ`. Each
    run of letters is one letter repeated, each a date or time letter, with a separator between some."""
    if not re.fullmatch(r"[A-Za-z]+(?:[-/:.,_][A-Za-z]+)+", code):
        return False
    runs = [m.group(0) for m in re.finditer(r"([A-Za-z])\1*", re.sub(r"[^A-Za-z]", "", code))]
    return all(r[0] in "YyMDdHhmSsTZ" for r in runs)


def _labels_in(text: str, labels) -> list[str]:
    """Labels written in the text as themselves: "1.3" in "version 1.3", not in "11.3" or "1.30"."""
    out = []
    for label in sorted(labels, key=len, reverse=True):
        # A bare number ("1", "2") is everywhere in prose: as a label it needs "version" or "v" before it.
        cue = r"(?:\bversions?\s+|\bv)" if label.isdigit() else ""
        if re.search(rf"(?<![\w.])(?:[vV](?=\d))?{re.escape(label)}(?![\w]|\.\d)" if not cue else
                     rf"{cue}{re.escape(label)}(?![\w]|\.\d)", text, re.I if cue else 0):
            out.append(label)
    return out


def verify(answer: str, evidence, known=None, cover=None) -> Verification:
    """`cover`: comparisons or diffs whose breaking changes the answer must mention (they are evidence too).
    `known`: text, or names, the answer may repeat without the evidence holding them."""
    cover = list(cover or [])
    ev = _Evidence(list(evidence) + [c for c in cover if not any(c is e for e in evidence)])
    known_words = set().union(*(_words_of(str(k)) for k in known)) if known else set()
    findings: list[Finding] = []

    cited = [node_id for node_id, _ in _cited_ids(answer)]
    for node_id in cited:
        if node_id not in ev.ids:
            findings.append(Finding("unknown-citation", node_id, "this id is not in the evidence"))

    names = [n for n in _names(answer) if n not in _COMMON]
    def known(word):  # a sentence may start with it capitalised, or not: SendSMS / sendSMS
        return any(w in ev.words or w in known_words for w in (word, word[:1].swapcase() + word[1:])) \
            or word in _COMMON

    # `Pet*`: a family of names, known when the evidence has a name it begins
    families = set(re.findall(r"`([A-Za-z_][\w.]*)\*`", answer))
    unknown = [n for n in names if not known(n) and not all(known(p) for p in n.split("."))
               and not (n in families and any(w.startswith(n) for w in ev.words))]

    claims, absent, previous, previous_in = 0, set(), [], []
    for sentence in (s.strip() for s in _SENTENCE.split(_REFERENCE.sub(" ", _without_ids(answer))) if s.strip()):
        in_sentence = _labels_in(sentence, ev.labels)
        pending = None  # a claim word with nothing named yet: "Since 1.2, Pet has tag"
        for clause in _clauses(sentence):
            labels = here_labels = _labels_in(clause, ev.labels)
            here = _subjects(clause, ev, labels)
            # What a later "it" or "these" refers to, and where: a name the evidence has, not one it does not
            # (an invented name, a chunk reference), which is reported as unknown already.
            named_here = [x for x in here if x not in ev.schemas]
            if named_here:
                previous = [x for x in named_here if known(x)]
                previous_in = list(dict.fromkeys(t for t in _IDENT.findall(clause) if t in ev.schemas))
            if not labels and len(in_sentence) == 1:
                labels = in_sentence  # "In 1.2, Pet has no tag": the version is in another clause
            if not labels:
                continue
            prose = _CODE.sub(" ", clause)  # a field named `added` or `since` says nothing was added
            kind = ("missing" if _MISSING.search(prose) else "removed" if _REMOVED.search(prose)
                    else "added" if _ADDED.search(prose) else "changed" if _CHANGED.search(prose) else None)
            subjects = here if labels == here_labels else _subjects(clause, ev, labels)
            if kind is None and pending and subjects:
                kind, labels = pending
            pending = None
            if kind is None:
                continue
            inside = _added_inside(clause) if kind == "added" else None
            if inside:  # "`V` was added to the `x` enum": a change in x, not x being added
                family, name = inside
                if name:
                    claims += 1
                    inner = [t for t in _IDENT.findall(_CODE.sub(lambda m: m.group(1), clause))
                             if t in ev.schemas and t != name]
                    findings += _check_inside(clause.strip(), family, name, inner, labels, ev)
                continue
            pronoun = not subjects and re.search(r"\b(it|they|them|this|that|these|those)\b", clause, re.I)
            if not subjects and not pronoun:
                pending = (kind, labels)
                continue
            if pronoun:
                if kind in ("missing", "removed") and not any(
                        _negated(clause, p, [], kind) for p in re.findall(r"\b(it|they|them|this|that|these|those)\b",
                                                                          clause, re.IGNORECASE)):
                    continue  # "it had no constraints", "no mechanism for securing it": about what it has
                subjects = previous  # "Pet includes `tag`, but 1.2 lacks it": it is tag, in Pet
            # A schema named beside something else is where it is, not what the claim is about:
            # in "tag was added to Pet", tag is the subject and Pet the container.
            named = list(dict.fromkeys(t for t in _IDENT.findall(clause) if t in ev.schemas))
            others = [x for x in subjects if x not in named]
            containers = named if others else []
            subjects = others or subjects
            if kind == "added":  # "`kind` gained `GIFTCARD`": kind holds it, GIFTCARD is new
                held = [x for x in subjects if _holds(clause, x, subjects)]
                if held:  # when every name holds something, "`kind` and `category` gained new values" names nothing new
                    containers = list(dict.fromkeys(containers + held))
                    subjects = [x for x in subjects if x not in held]
            if pronoun and not containers:
                containers = previous_in
            if not subjects:
                continue
            if kind in ("missing", "removed") and not pronoun:
                # "no encryption is applied to `tag`" says nothing about whether tag is there.
                subjects = [x for x in subjects if _negated(clause, x, subjects + containers, kind)]
            if not subjects:
                continue
            if kind == "removed":
                labels = _removed_in(clause, labels, ev.labels)
                if not labels:
                    continue
            if kind in ("added", "removed") and len(labels) > 1:
                labels = [max(labels, key=label_key)]  # "removed between 1.2 and 1.10": a change in 1.10
            claims += 1
            claim_findings = _check_claim(kind, clause.strip(), subjects, containers, labels, ev)
            findings += claim_findings
            if kind in ("missing", "removed") and not claim_findings:
                absent.update(subjects)  # rightly said to be absent: not an invented name
    findings[0:0] = [Finding("unknown-name", n, "this name is nowhere in the evidence") for n in unknown
                     if n not in absent]
    required_findings, required_claims = _check_required(answer, ev)
    findings += required_findings
    limit_findings, limit_claims = _check_limits(answer, ev)
    findings += limit_findings
    count_findings, counts = _check_counts(answer, ev)
    omitted, covered = _omitted(answer, cover)
    return Verification(findings + count_findings + omitted, len(cited), len(names), claims, counts, covered,
                        required_claims, limit_claims)


# ---- coverage ------------------------------------------------------------------------------------

def _omitted(answer: str, cover: list) -> tuple[list[Finding], int]:
    """Breaking changes in the comparisons and diffs to cover that the answer names nothing of. The same change
    reached from many places (a field every operation returns) is one change."""
    from .compare import Change  # compare needs query, which verify needs: imported when used

    text, said = _without_ids(answer), {}
    groups: dict[tuple, tuple[dict, dict, list[str]]] = {}
    for item in cover:
        d = item.to_dict() if hasattr(item, "to_dict") else item
        if not isinstance(d, dict) or "a_label" not in d:
            continue
        if "changes" in d:  # one schema, operation or field
            pairs = [(d["name"], c) for c in d["changes"] if c.get("level") == "breaking"]
        else:  # a whole diff: what breaks, by operation
            pairs = [(op, {"kind": "operation-removed", "where": "", "level": "breaking", "a": None, "b": None})
                     for op in d.get("operations_removed", []) if op not in d.get("removed_after_deprecation", [])]
            pairs += [(op, c) for op, cs in (d.get("operations_changed") or {}).items() for c in cs
                      if c.get("level") == "breaking"]
        for place, c in pairs:
            terms = _changed_thing(c) or {place}
            k = (d["a_label"], d["b_label"], c.get("kind"), str(c.get("before")), str(c.get("after")),
                 tuple(sorted(terms)))
            groups.setdefault(k, (c, d, []))[2].append(place)
    # A value matches as written, or by its plain key when no other value in the changes has that key:
    # GIFT_CARD and GIFTCARD are two values, "gift card" names GIFT_CARD only when GIFTCARD is not there too.
    spellings: dict[str, set[str]] = {}
    of_field: dict[tuple, set[str]] = {}  # (place, where) -> the changed values of that field
    for c, _, places in groups.values():
        for v in _values_of(c):
            spellings.setdefault(key(v), set()).add(v)
            of_field.setdefault((places[0], c.get("where")), set()).add(v)
    out = []
    for (a, b, *_, terms), (c, d, places) in groups.items():
        # Naming the operation is not naming what changed in it: `clients` in "POST /clients/provision". The
        # place is left out only as a whole name (not Pet in `PetCat`), and only for a change inside it.
        inside = terms != (places[0],) and not _about_itself(c, places[0])
        k = (places[0], inside)
        if k not in said:
            said[k] = _said(re.sub(rf"(?<![\w.$@-]){re.escape(places[0])}(?![\w-])", " ", text) if inside else text)
        values = _values_of(c)

        def named(v):
            return v in said[k] or key(v) in said[k] and len(spellings[key(v)]) == 1

        if values and any(named(v) for v in of_field[(places[0], c.get("where"))]):
            # The answer names values of this field: it must name this one too ("gained `active`" leaves out
            # `closed`), not only the field.
            if any(named(v) for v in values):
                continue
        elif any(t in said[k] or key(t) in said[k] and (t not in values or len(spellings[key(t)]) == 1)
                 for t in terms):
            continue
        line = Change(c.get("kind", ""), c.get("where") or "", c.get("before"), c.get("after"), "breaking",
                      c.get("side") or "", c.get("a"), c.get("b"), c.get("note")).line()
        more = f" (and {len(places) - 1} more)" if len(places) > 1 else ""
        out.append(Finding("omitted-change", line, f"breaking from {a} to {b} in {places[0]}{more}; the answer "
                           "does not mention it", [a, b], list(dict.fromkeys(places))))
    return out, len(groups)


def _values_of(change: dict) -> set[str]:
    """The values an enum or const change is about, as written: 'GIFT_CARD'."""
    if change.get("kind", "").startswith("enum-value") or change.get("kind") == "const-changed":
        return {str(change[k]).strip("'\"") for k in ("before", "after") if change.get(k) not in (None, "none")}
    return set()


def _about_itself(change: dict, place: str) -> bool:
    """A change of the compared thing itself, with no place inside it: `Owner.addresses` removed."""
    ids = [str(change[k]) for k in ("a", "b") if change.get(k)]
    return not change.get("where") and bool(ids) and all(i.endswith("/" + place) for i in ids)


def _said(text: str) -> set[str]:
    """Names the text mentions: identifiers as written, and runs of one to four words by their plain key, so
    "birth date" mentions birthDate and "GET /pets" its operation."""
    out = set(_words_of(text))
    out.update(w.strip("`'\".,;:()[]{}*") for w in text.split())  # values as written: application/zip
    out.update(w.lower() for w in re.findall(r"[A-Za-z]+", text) if w.lower() in _WRAP_WORDS)
    tokens = re.findall(r"[A-Za-z0-9_]+", text)
    for size in range(1, 5):
        for i in range(len(tokens) - size + 1):
            out.add(key(" ".join(tokens[i:i + size])))
    for m in re.finditer(r"\b(GET|PUT|POST|DELETE|OPTIONS|HEAD|PATCH|TRACE)\s+(/\S*)", text):
        out.add(f"{m.group(1)} {m.group(2).rstrip('.,;:)`')}")
    return out


# ---- required or optional ----------------------------------------------------------------------

_REQUIRED_WORD = re.compile(r"\b(not\s+|no\s+longer\s+|n't\s+|never\s+)?(required|mandatory|optional)\b", re.I)
# Requiredness that depends on something, which the spec cannot show: "required when paying by card".
_CONDITION = re.compile(r"\b(?:if|when|whenever|unless|depending|depends|conditionally|only for|for some|"
                        r"in some|in certain|either|recommended)\b", re.I)


# A condition that only says the value is there, which every limit assumes: "if the field is present", "when
# provided". It does not make a limit conditional.
_PRESENCE = re.compile(r"\b(?:if|when|whenever)\s+(?:(?:it|they|the\s+\w+|this\s+\w+|`[^`]+`)(?:\s+\w+)?\s+"
                       r"(?:is|are)\s+)?(?:present|provided|included|supplied|sent|given|set|specified)"
                       r"(?=\s*(?:[,.;:)]|$|\b(?:and|or|but|then|it|they|the|its|their)\b))",
                       re.IGNORECASE)


_NOW = re.compile(r"\b(?:new|newly|now|became|becomes|no longer|added|introduced|since)\b", re.I)


# A record line saying whether the name above it is required: "- Required: Yes"
_REQUIRED_FLAG = re.compile(r"\s*(?:[-*\u2022]|\d+[.)])\s+required\s*:\s*(yes|no|true|false)\.?\s*", re.IGNORECASE)


def _check_required(answer: str, ev: _Evidence) -> tuple[list[Finding], int]:
    """Claims that a field or parameter is required, or optional, that the evidence shows the other way."""
    if not ev.required:
        return [], 0
    out, checked = [], 0
    section, heading = None, None
    for sentence in (s.strip() for s in _SENTENCE.split(_REFERENCE.sub(" ", _without_ids(answer))) if s.strip()):
        if _SECTION.fullmatch(sentence):  # "#### Acme 1.0": the lines below are about that version
            in_heading = _labels_in(sentence, ev.labels)
            section = in_heading[0] if len(in_heading) == 1 else None
        sentence = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", sentence)
        in_sentence = _labels_in(sentence, ev.labels)
        record = _HEADING.fullmatch(sentence) or _RECORD.fullmatch(sentence)
        if record:  # "- Field: `nickname`", then "- Required: Yes"
            heading = record.group(1)
            continue
        flag = _REQUIRED_FLAG.fullmatch(sentence) if heading and _BULLET.match(sentence) else None
        if not _BULLET.match(sentence):
            heading = None
        for clause in [sentence] if flag else _clauses(sentence):
            prose = _CODE.sub(" ", clause)  # `required`, the keyword, says nothing
            said = {flag.group(1).lower() in ("yes", "true")} if flag else \
                {not bool(m.group(1)) if m.group(2).lower() != "optional" else bool(m.group(1))
                 for m in _REQUIRED_WORD.finditer(prose)}
            if len(said) != 1 or _CONDITION.search(prose) or _COUNT.search(clause) or _MORE.search(clause):
                continue
            claim = said.pop()
            labels = _labels_in(clause, ev.labels) or (in_sentence if len(in_sentence) == 1 else []) or \
                ([section] if section else [])
            for subject in [_path_name(heading)] if flag else _required_subjects(clause):
                owner, _, member = subject.rpartition(".")
                shown = ev.required_in.get((owner, member)) if owner else ev.required.get(subject)
                if not shown:
                    continue
                looked = {l: v for l, v in shown.items() if not labels or l in labels}
                if not labels and len(looked) > 1 and _NOW.search(prose):
                    # "a new optional `tag`", "`tag` is now required": said of the latest version
                    latest = max(looked, key=label_key)
                    looked = {latest: looked[latest]}
                if not looked:
                    continue
                checked += 1
                if all(v == {not claim} for v in looked.values()):
                    where = ", ".join(sorted(looked, key=label_key))
                    out.append(Finding("contradicted-claim", clause.strip(), f"the evidence shows {subject} "
                                       f"{'optional' if claim else 'required'} in {where}", sorted(looked)))
    return out, checked


# Words that may stand between a name and "required" or "optional" said of it: "`limit` is now an optional query
# parameter", "a required header added: `X-Trace`".
_TIES = {"is", "are", "was", "were", "be", "becomes", "became", "stays", "remains", "now", "still", "also",
         "always", "a", "an", "the", "not", "no", "longer", "never", "n't", "isn", "t", "field", "property",
         "parameter", "header", "attribute", "query", "path", "cookie", "body", "request", "response", "added",
         "new", "string", "integer", "number", "boolean", "array", "object"}


# Plain words that, right after "required" or "optional", name being required, not a field: "the required status
# changed", "optional flags updated". Written as code, or followed by "field", they are the field: "a required
# `status`", "a required status field".
_REQUIREDNESS = {"status", "state", "flag", "flags", "setting", "settings", "marker", "markers", "keyword",
                 "keywords", "constraint", "constraints", "rule", "rules", "list", "lists", "array", "arrays",
                 "attribute", "attributes", "set", "ness", "info", "information", "metadata", "semantics"}
_KIND_WORDS = {"field", "fields", "property", "properties", "parameter", "parameters", "attribute", "header", "key"}


# Plain words that are never what "required" is said of, even when a field has that name: "required in `Owner`",
# "required since 2.0"
_NOT_REQUIRED_SUBJECT = _FUNCTION | {"since", "until", "after", "before", "during", "unless", "except", "under",
                                     "over", "between", "across", "whenever", "while"}


def _required_subjects(clause: str) -> list[str]:
    """The names "required" or "optional" is said of: right before it ("`limit` is optional") or right after
    it ("a required `limit` parameter"), with only words like "is", "a", "parameter" between. Not the path of an
    operation named beside it."""
    text = re.sub(r"\b(?:GET|PUT|POST|DELETE|OPTIONS|HEAD|PATCH|TRACE)\s+/\S*|(?<![\w`])/[\w{}/.-]*", " ", clause)
    out = []
    code = [(m.start(), m.end(), _path_name(m.group(1))) for m in _CODE.finditer(text)]
    names = sorted(code + [(m.start(), m.end(), m.group(0)) for m in _IDENT.finditer(_CODE.sub(
        lambda m: " " * len(m.group(0)), text)) if not _REQUIRED_WORD.fullmatch(m.group(0))
        and m.group(0).lower() not in _TIES | _NOT_REQUIRED_SUBJECT])
    names = [n for n in names if n[2]]
    for q in _REQUIRED_WORD.finditer(_CODE.sub(lambda m: "_" * len(m.group(0)), text)):
        before = [n for n in names if n[1] <= q.start()]
        after = [n for n in names if n[0] >= q.end()]
        for side, near, gap in (("before", before[-1:], lambda n: text[n[1]:q.start()]),
                                ("after", after[:1], lambda n: text[q.end():n[0]])):
            if near and all(w.lower() in _TIES for w in re.findall(r"[A-Za-z']+", gap(near[0]))):
                if side == "after" and near[0] not in code and near[0][2].lower() in _REQUIREDNESS:
                    following = re.match(r"\W*([A-Za-z]+)", text[near[0][1]:])
                    if not following or following.group(1).lower() not in _KIND_WORDS:
                        continue  # "the required status changed": being required, not a field
                out.append(near[0][2])
    return list(dict.fromkeys(out))


def _path_name(code: str) -> str:
    """The name a code span is about: `Pet.tag` as written, the last part of a path through arrays or
    several levels (`pets[].tag` -> tag), nothing for code that is not a name."""
    code = code.strip()
    if _IDENT.fullmatch(code):
        return code if code.count(".") <= 1 else code.rsplit(".", 1)[1]
    parts = [p for p in re.split(r"[.\[\]]+", code) if p]
    return parts[-1] if parts and all(_IDENT.fullmatch(p) for p in parts) else ""


# ---- limits -------------------------------------------------------------------------------------

_N = r"(-?\d[\d,]*(?:\.\d+)?)(?!\w|[.,]\d)"
_UNIT = r"(characters?|chars?|digits?|letters?|items?|elements?|entries|entry)"
_MAX = (r"max(?:imum)?|maxLength|maxItems|at\s+most|up\s+to|no\s+more\s+than|not\s+more\s+than|"
        r"no\s+longer\s+than|not\s+longer\s+than|limited\s+to|(?:can(?:not|'t)|must\s+not|may\s+not|does\s+not)"
        r"\s+exceed")
_MIN = r"min(?:imum)?|minLength|minItems|at\s+least|no\s+(?:fewer|less)\s+than|not\s+(?:fewer|less)\s+than"
_OF = r"(?:\s+(?:length|value|size|number\s+of\s+(?:characters|items|elements)))?(?:\s*(?:of|is|:|=))?\s*"
# "maximum 256 characters", "maxLength: 3", "up to 50 items", "at least 1 character"
_LIMIT = re.compile(rf"(?<![\w`-])({_MAX}|{_MIN})(?![\w-]){_OF}{_N}(?:\s+{_UNIT}\b)?", re.I)
# "10 characters max", "3 items or fewer"
_LIMIT_AFTER = re.compile(rf"(?<![\w.]){_N}\s+{_UNIT}\s+(max(?:imum)?|at\s+most|or\s+(?:fewer|less)|"
                          rf"min(?:imum)?|at\s+least|or\s+more)\b", re.I)
# "the maximum length of `name` is 40 (characters)"
# What may stand between a name and "is N": where it is ("in the `Owner` schema", "in 2.0", "(2.0)"), or a
# word saying what it is ("field", "header")
# A version may be named with a word before its number: "in Acme 2.0", "in version 2.0"
_VERSION_NAMED = r"(?:[A-Z][\w-]*\s+|version\s+)?v?\d[\w.\-]*"
_QUALIFIER = (rf"\s+(?:in|on|of|within|for)\s+(?:the\s+|an?\s+)?(?:`[^`]+`|{_VERSION_NAMED}|[A-Z]\w*)"
              r"(?:\s+(?:schema|object|operation|endpoint|model|type|request|response|version))?|\s+\([^)]*\)")
_IN_SCHEMA = rf"(?:{_QUALIFIER}|\s+(?:field|property|parameter|header|attribute|array|string|value)){{0,4}}"
_LIMIT_OF = re.compile(rf"(?<![\w`-])(max(?:imum)?|min(?:imum)?)\s+(length|value|size|number\s+of\s+(?:items|elements))"
                       rf"\s+(?:of|for)\s+(?:the\s+)?(?:`[^`]+`|[A-Za-z_][\w.]*){_IN_SCHEMA}\s+(?:is|=|:)\s+{_N}"
                       rf"(?:\s+{_UNIT}\b)?", re.I)
# "the minimum `age` in `Owner` is 0"
_LIMIT_NAMED = re.compile(rf"(?<![\w`-])(max(?:imum)?|min(?:imum)?)\s+(?:the\s+)?`[^`]+`{_IN_SCHEMA}\s+(?:is|=|:)\s+{_N}"
                          rf"(?:\s+{_UNIT}\b)?", re.I)
# "exactly 3 characters": both limits
_EXACTLY = re.compile(rf"\bexactly\s+{_N}\s+{_UNIT}\b", re.I)
# "at least one `tags` entry": a count of the items of the name right after
_COUNT_WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten".split())}
_COUNT_OF = re.compile(rf"(?<![\w`-])(at\s+least|at\s+most|up\s+to|no\s+more\s+than|no\s+fewer\s+than)\s+"
                       rf"(\d+|{'|'.join(_COUNT_WORDS)})\s+`[^`]+`", re.I)
# "between 1 and 10 characters", "from 0 to 120"
_BETWEEN_LIMITS = re.compile(rf"\b(?:between\s+{_N}\s+and|from\s+{_N}\s+(?:to|through))\s+{_N}"
                             rf"(?:\s+{_UNIT}\b|(?=\s*\(?(?:inclusive)?\)?\s*[.;]?\s*$))", re.I)
_ROUGHLY = re.compile(r"\b(?:about|around|roughly|approximately|nearly|almost|over|under|typically|usually|"
                      r"recommended|suggested|ideally)\s*$", re.I)
_NO_LIMIT = re.compile(r"\b(?:no|not|without|any)\s+(?:a\s+)?(?:max(?:imum)?|min(?:imum)?|limit)", re.I)
# "has no maximum length", "without any defined minimum or maximum constraints", "no length limit"
_LIMIT_WORD = r"max(?:imum)?|min(?:imum)?|maxLength|minLength|maxItems|minItems"
_ABSENT = re.compile(rf"\b(?:no|without(?:\s+(?:a|any))?|(?:does|do)\s+not\s+have\s+(?:a|any))\s+"
                     r"(?:defined\s+|explicit\s+|documented\s+|specified\s+|stated\s+|set\s+)?"
                     rf"(?:((?:{_LIMIT_WORD})(?:\s*(?:,|or|and|/|nor)\s*(?:{_LIMIT_WORD}))*)"
                     r"(?:\s+(length|value|size|number\s+of\s+(?:items|elements)|items))?"
                     r"(?:\s+(?:limits?|constraints?|bounds?))?"
                     r"|(length\s+)?(?:limits?|constraints?|bounds?))\b", re.I)
# Words that may stand between a name and the limit said of it: "`nickname` has a maximum length of 40",
# "`nickname`: maximum 40", "`a` and `b` are at most 100 characters each"
_LIMIT_TIES = {"is", "are", "was", "were", "be", "has", "have", "had", "a", "an", "the", "its", "of", "with",
               "must", "can", "may", "should", "will", "accepts", "accept", "takes", "allows", "allow", "holds",
               "hold", "contains", "contain", "field",
               "fields", "property", "parameter", "parameters", "header", "value", "values", "length", "string",
               "strings", "array", "list", "each", "now", "only", "also", "and", "or", "both", "in", "v",
               # what a value is, and where: "is an integer (`int32`) with", "in the `Owner` schema allows"
               "integer", "number", "boolean", "object", "schema", "operation", "request", "response", "body",
               "query", "path", "cookie", "endpoint", "defined", "declared", "specified", "as", "type", "on", "for", "it",
               "this", "that", "limit", "limits", "constraint", "constraints", "attribute"}


def _limits_said(clause: str) -> list[tuple[int, str, float, str, int]]:
    """Each limit a clause states: (where it starts, "max" or "min", the number, what it counts: "length",
    "items" or "" when it does not say, where it ends)."""
    def unit(u):
        u = (u or "").lower()
        return "" if not u else "items" if u[:4] in ("item", "elem", "entr") else "length"

    def num(s):
        return float(s.replace(",", ""))

    out = []
    for m in _LIMIT.finditer(clause):
        if _ROUGHLY.search(clause[:m.start(2)]) or _ROUGHLY.search(clause[m.start(1):m.start(2)]):
            continue
        word = m.group(1).lower()
        u = unit(m.group(3)) or ("length" if "length" in word or re.search(r"length|characters", m.group(0), re.I)
                                 else "items" if "items" in word else "")
        out.append((m.start(), "min" if word.startswith(("min", "at least", "no ", "not f", "not l")) and not
                    word.startswith(("not longer", "not more", "no more", "no longer")) else "max", num(m.group(2)), u,
                    m.end()))
    for m in _LIMIT_AFTER.finditer(clause):
        word = m.group(3).lower()
        out.append((m.start(), "min" if word.startswith(("min", "at least", "or more")) else "max",
                    num(m.group(1)), unit(m.group(2)), m.end()))
    for m in _LIMIT_OF.finditer(clause):
        if not _ROUGHLY.search(clause[:m.start(3)]):
            word = m.group(2).lower()
            out.append((m.start(), "min" if m.group(1).lower().startswith("min") else "max", num(m.group(3)),
                        unit(m.group(4)) or ("length" if word == "length" else "items" if "items" in word or
                                             "elements" in word else ""), m.end()))
    for m in _LIMIT_NAMED.finditer(clause):
        if not _ROUGHLY.search(clause[:m.start()]):
            out.append((m.start(), "min" if m.group(1).lower().startswith("min") else "max", num(m.group(2)),
                        unit(m.group(3)), m.end()))
    for m in _COUNT_OF.finditer(clause):
        if not _ROUGHLY.search(clause[:m.start()]):
            n = m.group(2).lower()
            word = m.group(1).lower()
            out.append((m.start(), "min" if word.startswith(("at least", "no fewer")) else "max",
                        float(_COUNT_WORDS.get(n, n) if n in _COUNT_WORDS else num(n)), "items", m.end()))
    for m in _BETWEEN_LIMITS.finditer(clause):
        out += [(m.start(), "min", num(m.group(1) or m.group(2)), unit(m.group(4)), m.end()),
                (m.start(), "max", num(m.group(3)), unit(m.group(4)), m.end())]
    for m in _EXACTLY.finditer(clause):
        out += [(m.start(), "min", num(m.group(1)), unit(m.group(2)), m.end()),
                (m.start(), "max", num(m.group(1)), unit(m.group(2)), m.end())]
    return out


_KEYWORDS = {("max", "length"): ("maxLength",), ("max", "items"): ("maxItems",),
             ("max", ""): ("maxLength", "maxItems", "maximum"),
             ("min", "length"): ("minLength",), ("min", "items"): ("minItems",),
             ("min", ""): ("minLength", "minItems", "minimum")}


# The word before a version's number names the version: "in Acme 2.0", "for Acme v2"
_VERSION_WORD = re.compile(r"\b((?:in|for|of|on|under)\s+)([A-Z][\w-]*)(?=\s+v?\d)")


def _limit_subjects(clause: str, at: int, said=(), end: int | None = None) -> list[str]:
    """The names a limit at `at` is said of: right before it, a list of names ("`a` and `b`: max 100"), with
    only words like "is", "has a" between, or other limits said ("a minimum of 1 and a maximum of 40"); else
    the name right after it ("the maximum length of `name`", "the minimum `age`", "at least one `tag`", "a
    minItems 1 constraint on the `tags` array")."""
    counted = _COUNT_OF.match(clause, at)
    if counted:  # "at least one `tag`": the name counted
        return [_path_name(re.search(r"`([^`]+)`$", counted.group(0)).group(1))]
    for start, stop in said:  # other limits said in the clause stand between a name and this one like "is"
        if stop <= at:
            clause = clause[:start] + " " * (stop - start) + clause[stop:]
    spans = [(m.start(), m.end(), _limit_name(m.group(1))) for m in _CODE.finditer(clause)]
    # code that names nothing (`GET /owners`) and what is in brackets ("(2.0)") stand aside like "is"
    for start, stop, name in spans:
        if not name:
            clause = clause[:start] + " " * (stop - start) + clause[stop:]
    clause = re.sub(r"\([^)]*\)", lambda m: " " * len(m.group(0)), clause)
    clause = _VERSION_WORD.sub(lambda m: m.group(1) + " " * len(m.group(2)), clause)
    plain = _CODE.sub(lambda m: " " * len(m.group(0)), clause)
    spans += [(m.start(), m.end(), m.group(0)) for m in _IDENT.finditer(plain)
              if m.group(0).lower() not in _LIMIT_TIES and not _LIMIT.match(plain, m.start())
              and re.search(r"[a-z][A-Z]|_|\.", m.group(0))]  # plain words are prose: only names shaped like code
    spans = sorted(s for s in spans if s[2])

    def ties(text):
        return all(w.lower() in _LIMIT_TIES for w in re.findall(r"[A-Za-z']+", text))

    before = [s for s in spans if s[1] <= at]
    out, edge = [], at
    for s in reversed(before):
        if not ties(clause[s[1]:edge]):
            break
        out.insert(0, s[2])
        edge = s[0]
    if out:
        return out
    after = re.match(rf"\s*(?:{_MAX}|{_MIN})(?:\s+(?:length|value|size|number\s+of\s+\w+))?\s+(?:of|for)\s+"
                     r"(?:the\s+)?(`[^`]+`|[A-Za-z_][\w.]*)", clause[at:], re.I) or \
        re.match(rf"\s*(?:{_MAX}|{_MIN})(?:\s+(?:\d+|{'|'.join(_COUNT_WORDS)}))?\s+(?:the\s+)?(`[^`]+`)",
                 clause[at:], re.I)
    if not after and end is not None:
        after = re.match(r"\s*(?:constraints?|limits?|rules?|bounds?)?\s*(?:on|for|of|in)\s+(?:the\s+)?(`[^`]+`)",
                         clause[end:], re.I)
    if after:
        name = after.group(1)
        return [_limit_name(name[1:-1]) if name.startswith("`") else name]
    return []


def _limit_name(code: str) -> str:
    """The name of a field or parameter in code: as _path_name, and a header's name (`X-Trace-Id`)."""
    code = code.strip()
    return code if re.fullmatch(r"[A-Za-z][\w]*(?:-[\w]+)+", code) else _path_name(code)


# A limit keyword or a number written as code reads as plain words: "a `maxLength` of 40", "Maximum: `120`",
# "the `minItems 1` constraint"
_CODED_LIMIT = re.compile(rf"`((?:{_LIMIT_WORD})(?:\s*[:=]?\s*-?\d[\d,]*(?:\.\d+)?)?|-?\d[\d,]*(?:\.\d+)?)`", re.I)
# A line naming what the lines below it are about: "`age`:" or "**`age`**"
_HEADING = re.compile(r"[\s#*-]*\**`([^`]+)`\**\s*:?\s*$")
_BULLET = re.compile(r"\s*(?:[-*\u2022]|\d+[.)])\s")
# A record line naming what the next lines are about: "- **Field:** `nickname`"
_RECORD = re.compile(r"[\s#*-]*(?:field|name|parameter|property|header|attribute)\s*:\s*`([^`]+)`\s*$", re.I)
# A heading line, which may name the version the lines below it are about: "#### Acme 1.0", "**Version 2.0**"
_SECTION = re.compile(r"\s*(?:#{1,6}\s+.+|\*\*[^*\n]+\*\*\s*:?)\s*")
# A sentence about the name just before: "It is a string ...", "The field has ..."
_PRONOUN = re.compile(r"\s*this\s+means(?:\s+that)?(?:\s+(?:it|the\s+(?:field|property|parameter|header|value|"
                      r"attribute|array|list|string)))?\b|\s*(?:it|its|it's|this|that)\b|\s*the\s+(?:field|property|parameter|header|value|attribute|"
                      r"array|list|string)\b", re.I)
# Words anywhere in a sentence that point back to the field named before: "Although the field is optional, ..."
_REFERS = re.compile(r"\b(?:the|this|that)\s+(?:field|property|parameter|header|attribute)\b", re.IGNORECASE)
# A verb joined to the sentence's subject: "`tags` is used by `GET /owners` and has a maximum of 8"
_JOINED = re.compile(r"\b(?:and|but)\s+(?:(?:also|still|then)\s+)?(?:has|have|is|are|must|can|may|allows?|accepts?|"
                     r"takes?|holds?|contains?)\b[^`]*$", re.IGNORECASE)
# A field in plain words, with the schema it is in: "the minimum age in `Owner` is 0"
_LIMIT_PLAIN = re.compile(rf"(?<![\w`-])(max(?:imum)?|min(?:imum)?)\s+((?:[A-Za-z]+\s+){{0,2}}?[A-Za-z]+?)"
                          r"(?:\s+(length|value|size|count))?\s+(?:in|for|of|on)\s+(?:the\s+|an?\s+)?`?([A-Z]\w*)`?"
                          rf"(?:\s+(?:schema|object))?(?:\s+(?:in|for)\s+{_VERSION_NAMED}|\s+\([^)]*\))?\s+(?:is|=|:)\s+{_N}(?:\s+{_UNIT}\b)?", re.I)


def _shown_for(ev: _Evidence, subject: str, schemas: list[str], labels: list[str]) -> dict:
    """The limits the evidence shows for a name, in the schema it is written with when there is one."""
    owner, _, member = subject.rpartition(".")
    shown = None
    for o in ([owner] if owner else []) + schemas:
        shown = shown or ev.limits_in.get((o, member or subject))
    shown = shown or ev.limits.get(member or subject)
    return {lab: v for lab, v in (shown or {}).items() if not labels or lab in labels}


def _subjects_at(clause: str, at: int, said, lead, lead_name, end: int | None = None, pronoun=None) -> list[str]:
    """The names a limit at `at` is said of, or else the name the sentence, its heading or "it" is about."""
    before = clause[:at].replace(lead.group(0).strip(), " ", 1) if lead else clause[:at]
    if pronoun and clause.startswith(pronoun.group(0)):  # "This means the value must be ...": "it" says who
        before = " " * pronoun.end() + before[pronoun.end():]
    for start, stop in said:
        if stop <= at:
            before = before[:start] + " " * (stop - start) + before[stop:]
    before = re.sub(r"\([^)]*\)", " ", before)
    return _limit_subjects(clause, at, said, end) or ([_limit_name(lead_name)] if lead_name and all(
        w.lower() in _LIMIT_TIES - {"value", "values"} for w in re.findall(r"[A-Za-z']+", before)) else [])


def _shown_as(values: dict) -> str:
    return "; ".join(f"{k} {' or '.join(_number_text(x) for x in sorted(v))} in {lab}"
                     for (lab, k), v in sorted(values.items(), key=lambda x: label_key(x[0][0])))


def _check_limits(answer: str, ev: _Evidence) -> tuple[list[Finding], int]:
    """Claims of a field's or parameter's limit ("`nickname`: maximum 40 characters") where the evidence shows
    that limit with another value, and claims that it has none ("`nickname` has no maximum length") where the
    evidence shows one. A limit the evidence does not show is not checked: it may be in prose only."""
    if not ev.limits:
        return [], 0
    out, checked = [], 0
    heading, previous, section = None, None, None
    for sentence in (s.strip() for s in _SENTENCE.split(_REFERENCE.sub(" ", _without_ids(answer))) if s.strip()):
        if _SECTION.fullmatch(sentence):  # "#### Acme 1.0", "**Version 2.0**": the lines below are about it
            in_heading = _labels_in(sentence, ev.labels)
            section = in_heading[0] if len(in_heading) == 1 else None
        sentence = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", sentence)  # bold says nothing
        sentence = _CODED_LIMIT.sub(lambda m: m.group(1), sentence)
        in_sentence = _labels_in(sentence, ev.labels)
        # "`homePage`: must be a URL, maximum 200 characters": the name before the colon is what the
        # whole sentence is about; a line that is only a name, or "Field: `name`", is what the bullets below it
        # are about; "it" is the one name with limits in the sentence before
        lead = re.match(r"[\s*-]*`([^`]+)`\s*:", sentence)
        lead_name = lead.group(1) if lead else None
        named = list(dict.fromkeys(n for n in (_limit_name(c) for c in _CODE.findall(sentence)) if n in ev.limits))
        it, previous = previous, (named[0] if len(named) == 1 else None)
        record = _HEADING.fullmatch(sentence) or _RECORD.fullmatch(sentence)
        if record:
            heading = record.group(1)
            continue
        if sentence.endswith(":") and len(named) == 1 and not _limits_said(sentence):
            heading = named[0]  # "The `age` field has the following range:" and the bullets below it
            continue
        if heading and _BULLET.match(sentence) and not lead:
            lead_name = heading
            previous = previous or (heading if not named else None)  # "This means the value ..." after them
        elif not _BULLET.match(sentence):
            heading = None
        pronoun = None
        if not lead_name and it:
            pronoun = _PRONOUN.match(sentence)
            lead_name = it if pronoun else None
        referred = None
        if not lead_name and it and not named and _REFERS.search(sentence):  # "Although the field is optional, ..."
            referred = previous = it
        # The one field the sentence names, when the sentence opens with it, is what a later clause with no name
        # of its own is about: "`tags` is used by two operations, and the `minItems 1` rule applies to it"
        opens = re.match(r"[\s*-]*(?:the\s+)?`([^`]+)`", sentence, re.IGNORECASE)
        own = named[0] if len(named) == 1 and not lead_name and opens and _limit_name(opens.group(1)) == named[0] \
            else None
        if any(re.match(r"\s*(?:if|when|whenever|unless)\b", c, re.IGNORECASE) and
               _CONDITION.search(_PRESENCE.sub(" ", _CODE.sub(" ", c))) for c in _clauses(sentence)):
            continue  # "When used for search, `name` has ...": the condition is a clause of its own
        for clause in _clauses(sentence):
            in_clause = {_limit_name(c) for c in _CODE.findall(clause)}
            clause_lead = lead_name or (own if own and own not in in_clause and not in_clause & ev.limits.keys()
                                        else None)
            if not clause_lead and referred and (_REFERS.search(clause) or re.search(r"\bits?\b", clause, re.IGNORECASE)):
                clause_lead = referred  # "Although the field is optional, it has ..."
            if _CONDITION.search(_PRESENCE.sub(" ", _CODE.sub(" ", clause))):
                continue
            labels = _labels_in(clause, ev.labels) or (in_sentence if len(in_sentence) == 1 else []) or \
                ([section] if section else [])
            schemas = [x for x in _IDENT.findall(_CODE.sub(lambda m: m.group(1), clause)) if x in ev.schemas]

            absent = _ABSENT.search(clause)
            if absent:  # "`x` has no maximum length": wrong when the evidence shows one
                said_words = (absent.group(1) or "").lower()
                sides = {s for s in ("max", "min") if s in said_words} or {"max", "min"}
                what = (absent.group(2) or ("length" if absent.group(3) or "length" in said_words else "")).lower()
                counts = "length" if "length" in what else "items" if "item" in what or "element" in what \
                    else "items" if "items" in said_words else ""
                named_after = re.match(r"\s*(?:(?:is|are)\s+(?:defined|set|specified|given|documented|stated)\s+)?"
                                       r"(?:for|on|of|in)\s+(?:the\s+)?`([^`]+)`", clause[absent.end():], re.I)
                subjects = _subjects_at(clause, absent.start(), (), lead, clause_lead) or \
                    ([_limit_name(named_after.group(1))] if named_after else [])
                for subject in subjects:
                    looked = _shown_for(ev, subject, schemas, labels)
                    keywords = {k for side in sides for k in _KEYWORDS[(side, counts)]}
                    values = {(lab, k): v for lab, kv in looked.items() for k, v in kv.items() if k in keywords}
                    if subject.rpartition(".")[2] in ev.limits or values:
                        checked += 1
                    if values:
                        out.append(Finding("contradicted-claim", clause.strip(),
                                           f"the evidence shows {subject}: {_shown_as(values)}",
                                           sorted({lab for lab, _ in values}, key=label_key)))
                continue
            if _NO_LIMIT.search(clause):
                continue
            said = [(s, None) for s in _limits_said(clause)]
            for m in _LIMIT_PLAIN.finditer(clause):  # "the minimum age in `Owner`": one field of Owner spells it
                spelled = [f"{o}.{f}" for o, f in ev.limits_in if o == m.group(4) and key(f) == key(m.group(2))]
                if len(spelled) == 1 and not _ROUGHLY.search(clause[:m.start()]):
                    u = (m.group(6) or "").lower()
                    counts = "items" if u[:4] in ("item", "elem", "entr") else "length" if u or \
                        (m.group(3) or "").lower() == "length" else ""
                    said.append(((m.start(), "min" if m.group(1).lower().startswith("min") else "max",
                                  float(m.group(5).replace(",", "")), counts, m.end()), spelled[0]))
            spans = [(s[0], s[4]) for s, _ in said]
            for (at, side, value, counts, end), spelled in said:
                subjects = [spelled] if spelled else \
                    _subjects_at(clause, at, [x for x in spans if x[0] != at], lead, clause_lead, end, pronoun)
                if not subjects and referred and not clause_lead and not in_clause & ev.limits.keys() and \
                        re.fullmatch(r"\s*(?:the|a|an)?\s*", _CODE.sub(" ", clause[:at]), re.IGNORECASE):
                    subjects = [referred]  # "..., the `minItems 1` rule means ...": the limit opens the clause
                if not subjects and own in in_clause and _JOINED.search(_CODE.sub(" ", clause[:at])) and all(
                        w.lower() in _LIMIT_TIES for w in re.findall(r"[A-Za-z']+", _JOINED.search(clause[:at])
                                                                     .group(0))):
                    subjects = [own]  # "... and has a `minItems` of 1": the verb's subject is the sentence's
                for subject in subjects:
                    looked = _shown_for(ev, subject, schemas, labels)
                    keywords = _KEYWORDS[(side, counts)]
                    values = {(lab, k): v for lab, kv in looked.items() for k, v in kv.items() if k in keywords}
                    if not values:
                        continue
                    checked += 1
                    if not any(value in v for v in values.values()):
                        out.append(Finding("contradicted-claim", clause.strip(),
                                           f"the evidence shows {subject}: {_shown_as(values)}",
                                           sorted({lab for lab, _ in values}, key=label_key)))
    return out, checked


def _number_text(x: float) -> str:
    return str(int(x)) if x == int(x) else str(x)


# ---- counts --------------------------------------------------------------------------------------

_NUMBER_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
                 "sixteen seventeen eighteen nineteen twenty").split()
_NUM = rf"(\d+|{'|'.join(_NUMBER_WORDS)})"
_NOUNS = {"fields": "fields", "field": "fields", "properties": "fields", "property": "fields",
          "attributes": "fields", "parameters": "parameters", "parameter": "parameters", "params": "parameters",
          "options": "options", "variants": "options", "alternatives": "options", "subtypes": "options"}
_QUALIFIERS = {"required": "required", "mandatory": "required", "optional": "optional", "inherited": "inherited",
               "own": "own", "direct": "own", "declared": "own", "unique": "own", "top-level": "total",
               "total": "total", "path": "path", "query": "query", "header": "header", "cookie": "cookie"}
_QUAL = "|".join(sorted(_QUALIFIERS, key=len, reverse=True))
_COUNT = re.compile(rf"(?<![\w.$/#-]){_NUM}\s+((?:(?:{_QUAL})\s+){{0,2}})({'|'.join(_NOUNS)})\b", re.I)
# "12 fields, 3 inherited": a second count of the same thing, its noun left out.
_MORE = re.compile(rf"(?<![\w.$/#-]){_NUM}\s+(?:of them\s+)?(required|mandatory|optional|inherited|own|direct|"
                   rf"declared|unique)\b(?!\s+(?:{'|'.join(_NOUNS)})\b)", re.I)
# Before the number, words that make it not an exact count of everything: "at least 3", "the first 2".
_NOT_EXACT = re.compile(r"\b(?:at least|at most|more than|fewer than|less than|over|under|about|around|roughly|"
                        r"approximately|nearly|almost|up to|some|first|last|top|next|previous|remaining|"
                        r"additional|extra|another|other|these|those|of)\s*$", re.I)
_DESCRIBED = re.compile(r"\b(?:it|this (?:schema|object|model|type|operation|endpoint)|"
                        r"the (?:schema|object|model|type|operation|endpoint|request))\b", re.I)


def _number(word: str) -> int:
    return int(word) if word.isdigit() else _NUMBER_WORDS.index(word.lower())


def _check_counts(answer: str, ev: _Evidence) -> tuple[list[Finding], int]:
    """Counts stated for a schema or operation in the evidence, against the counts its result shows."""
    if not ev.counts:
        return [], 0
    out, checked = [], 0
    for sentence in (s.strip() for s in _SENTENCE.split(_without_ids(answer)) if s.strip()):
        if _ADDED.search(sentence) or _REMOVED.search(sentence) or _CHANGED.search(sentence):
            continue  # "3 fields were added": a count of changes, not of what is there
        last_noun = None
        claims = []
        for m in _COUNT.finditer(sentence):
            noun = _NOUNS[m.group(3).lower()]
            qualifiers = {_QUALIFIERS[q.lower()] for q in m.group(2).split()}
            if re.search(r"\binherit\w*\s+(?:\w+\s+){0,2}$", sentence[:m.start()], re.I):
                qualifiers.add("inherited")  # "inherits 11 fields"
            if re.match(r"\s+in total\b", sentence[m.end():], re.I):
                qualifiers.add("total")
            after = re.match(r"\s+(?:is|are)\s+(?:all\s+)?(required|mandatory|optional|inherited)\b",
                             sentence[m.end():], re.I)
            if after:
                qualifiers.add(_QUALIFIERS[after.group(1).lower()])  # "only one field is required"
            if m.group(1).lower() == "one" and re.search(r"\b(?:is|was|as|be)\s+$", sentence[:m.start()], re.I):
                continue  # "name is one field you must send": not a count

            claims.append((m, noun, qualifiers))
            last_noun = noun
        if last_noun == "fields":
            taken = {m.start() for m, _, _ in claims}
            for m in _MORE.finditer(sentence):
                if m.start() not in taken:
                    claims.append((m, "fields", {_QUALIFIERS[m.group(2).lower()]}))
        for m, noun, qualifiers in claims:
            if len(qualifiers) > 1 or _NOT_EXACT.search(sentence[:m.start()]) or \
                    re.match(r"\s+(?:more|fewer|less)\b|\s+than\b", sentence[m.end():], re.I):
                continue  # "at least 3 fields", "4 fields more than its parent"
            parent = re.match(r"\s+(?:\w+\s+){0,3}?from\s+`?(" + _PART + ")", sentence[m.end():])
            things = _counted(sentence, m.start(), noun, ev, parent.group(1) if parent else None)
            if not things:
                continue
            qualifier = next(iter(qualifiers), None)
            allowed, shown = set(), []
            for t in things:
                counts = t.counts[noun]
                if qualifier and qualifier not in counts:
                    allowed = None
                    break
                if parent and parent.group(1) in t.parents:
                    allowed.add(t.parents[parent.group(1)])
                allowed |= {counts[qualifier]} if qualifier else {counts["total"], counts.get("own", counts["total"])}
                shown.append(f"{t.names[0]} in {t.label}: " + ", ".join(f"{n} {q}" for q, n in counts.items() if n
                                                                        or q == "total")
                             + (f", {t.parents[parent.group(1)]} from {parent.group(1)}"
                                if parent and parent.group(1) in t.parents else ""))
            if allowed is None:
                continue
            checked += 1
            stated = _number(m.group(1))
            if stated not in allowed:
                what = f"{qualifier} {noun}" if qualifier else noun
                out.append(Finding("wrong-count", sentence, f"{stated} {what} stated; the evidence shows "
                                   + "; ".join(shown), [t.label for t in things]))
    return out, checked


def _counted(sentence: str, at: int, noun: str, ev: _Evidence, parent: str | None) -> list[_Countable]:
    """What a count is about: the schema or operation named before it in the sentence (or after it), else
    the only one of its kind in the evidence when the sentence speaks of "it" or "the schema"."""
    kind = "operation" if noun == "parameters" else "schema"
    candidates = [c for c in ev.counts if c.kind == kind and noun in c.counts]
    if not candidates:
        return []
    labels = _labels_in(sentence, {c.label for c in candidates})
    if labels:
        candidates = [c for c in candidates if c.label in labels]
    tokens = list(_IDENT.finditer(sentence))
    def named(c, span):
        text = sentence[span[0]:span[1]]
        if kind == "operation":  # "GET /pets" is not named inside "GET /pets/{petId}"
            return any(re.search(rf"(?<![\w/{{}}-]){re.escape(n)}(?![\w/{{}}-])", text) for n in c.names)
        return any(t.group(0) in c.names and t.group(0) != parent and span[0] <= t.start() < span[1]
                   for t in tokens)
    for span in ((0, at), (at, len(sentence))):
        found = [c for c in candidates if named(c, span)]
        if found:
            # Another name between the thing and the number may be what is counted: "Pet's owner has 2 fields".
            return found if span[0] or not _names_between(sentence, found, at, ev) else []
    others = [n for n in _names(sentence) if n not in _COMMON and n != parent]
    names = {c.names[0] for c in candidates}
    if len(names) == 1 and not others and _DESCRIBED.search(sentence[:at]):
        return candidates
    return []


def _names_between(sentence: str, found: list[_Countable], at: int, ev: _Evidence) -> bool:
    end = max((sentence.rfind(n, 0, at) + len(n) for c in found for n in c.names if n in sentence[:at]), default=0)
    between = sentence[end:at]
    return any(n not in _COMMON and n not in ev.schemas and n not in c.names
               for n in _names(between) for c in found)


def _subjects(clause: str, ev: _Evidence, labels: list[str]) -> list[str]:
    """What a claim is about: names written as code, and names from the evidence written as words. An operation
    written as method and path is one name, and only the subject when nothing else is named ("GET /pets has no
    `tag`" is about tag)."""
    ops = [f"{m.group(1)} {trim_path(m.group(2))}" for m in _OPERATION.finditer(clause)]
    clause = _KIND_PREFIX.sub(" ", _OPERATION.sub(" ", clause))
    out = _named_subjects(clause, ev, labels)
    return out or list(dict.fromkeys(ops))


def _named_subjects(clause: str, ev: _Evidence, labels: list[str]) -> list[str]:
    quoted = [t for code in _CODE.findall(clause) for t in _IDENT.findall(code)]
    out = [n for n in _names(clause) if n not in _COMMON or n in quoted]  # `description` is a real field name
    # "the status field", "the birth date field" (birthDate): a name, unless the word only says which kind of
    # field ("the removed field") or is a function word ("the schema and field descriptions").
    out += _field_words(clause, ev)
    successor = _SUCCESSOR.search(clause)
    if successor:  # the new thing named after "replaced by" is not what the claim is about
        after = set(_IDENT.findall(clause[successor.end():]))
        out = [s for s in out if s not in after] or out
    paths = [c.strip() for c in _CODE.findall(clause) if _is_path(c.strip())]
    if paths:  # `pets[].tag` is one name, not pets and tag
        parts = {p for path in paths for p in _IDENT.findall(path)}
        out = paths + [n for n in out if n not in parts]
    out = list(dict.fromkeys(out))
    if out:
        return [s for s in out if not any(s == l for l in labels)]  # names written as code are the subject
    for token in _IDENT.findall(_CODE.sub(" ", clause)):
        if token in out or token in _COMMON or token.lower() in _FUNCTION:
            continue
        # A plain word is a subject only if the evidence has it as a name, not only in its prose.
        if token in ev.words and (token[0].isupper() or token in ev.anywhere() and token in ev.names):
            out.append(token)
    # a word of a version label is not a name: "streaming" in 0.0.0-streaming
    return [s for s in dict.fromkeys(out) if not any(s == l or s in re.split(r"[^A-Za-z0-9]+", l) for l in labels)]


# Verbs whose subject holds the new thing named after them: "`kind` gained `GIFTCARD`", "Pet now has `age`".
_HOLDS = re.compile(r"\b(?:gain(?:s|ed)?|now (?:has|have|includes|include|contains|contain|accepts|returns|"
                    r"allows))\b", re.I)


def _holds(clause: str, name: str, names: list[str]) -> bool:
    """Whether the name is the subject of such a verb: "`kind` gained", "`kind` and `category` now have"."""
    at = re.search(rf"(?<![\w.$@-]){re.escape(name)}(?![\w-])", clause)
    verb = _HOLDS.search(clause, at.end()) if at else None
    if not verb:
        return False
    between = clause[at.end():verb.start()]
    for other in names:
        between = re.sub(rf"(?<![\w.$@-]){re.escape(other)}(?![\w-])", " ", between)
    return not re.sub(r"[`\s,&]|\b(?:and|or|also|both)\b", "", between, flags=re.I)


def _negated(clause: str, subject: str, names: list[str], kind: str) -> bool:
    """Whether a claim that something is absent is about `subject` itself, not about something it has or that
    is done to it. True when the subject is not found as written (a name given in plain words)."""
    at = re.search(rf"(?<![\w.$@-]){re.escape(subject)}(?![\w-])", clause)
    if not at:
        return True
    follower = re.match(r"`?\s*([A-Za-z]+)", clause[at.end():])
    if follower and follower.group(1).islower() and follower.group(1) not in _AFTER_A_NAME:
        return False  # a word that describes the next one: "no encryption guidance", "lacks encryption fields"
    negation = _MISSING if kind == "missing" else _REMOVED
    before = [m for m in negation.finditer(clause) if m.end() <= at.start()]
    if not before:  # "`tag` is missing", "`tag` was removed"; not "`tag` has no default"
        return bool(_ABSENT_BEFORE.search(clause, at.end()))
    between = clause[before[-1].end():at.start()]
    for name in names:
        between = between.replace(name, " ")
    return all(w.lower() in _BETWEEN for w in re.findall(r"[A-Za-z]+", between))


def _field_words(clause: str, ev: _Evidence) -> list[str]:
    out, known = [], None
    for m in re.finditer(rf"\b({_PART})\s+(?:field|property|parameter|attribute)\b", clause):
        word = m.group(1)
        if word.lower() in _NOT_A_FIELD_NAME:
            continue
        if known is None:  # names of several words the evidence has, by their plain words
            known = {key(w): w for w in ev.words if len(words(w)) > 1}
        before = re.findall(r"[A-Za-z0-9]+", clause[:m.start()])[-3:]
        phrases = (" ".join(before[i:] + [word]) for i in range(len(before)))
        out.append(next((known[key(p)] for p in phrases if key(p) in known), word))
    return out


_ADDED_KINDS = ("added", "introduced")
_REMOVED_KINDS = ("removed", "dropped")


def _changed_thing(change: dict) -> set[str]:
    """What a change is about: the last name on its way (`owner.phone` -> phone), or the node it names."""
    out = set()
    where = str(change.get("where") or "").split(": ")[-1]
    last = re.split(r"[.\[\]{}]+", where.strip("[]{}."))[-1] if where else ""
    if last:
        out.add(last.split()[-1])
    for key in ("a", "b"):
        if change.get(key) and not where:
            out.add(re.split(r"[./#]", str(change[key]).split("/", 1)[1])[-1])
    kind = change.get("kind", "")
    for key in ("before", "after"):  # a renamed or replaced schema, an added option, a renamed operationId
        if change.get(key) and kind.startswith(("possibly-renamed", "option", "schema", "type-replaced", "operation-id")):
            out.add(str(change[key]))
    if where.startswith("security "):  # every scheme it names: "security A + B, C"
        out |= set(re.split(r"[\s,+]+", where[len("security "):])) - {""}
    if kind.startswith("enum-value") or kind == "const-changed":  # the value itself names the change
        out |= {str(change[k]).strip("'\"") for k in ("before", "after") if change.get(k) not in (None, "none")}
    if kind in ("options-unwrapped", "options-wrapped"):  # "{pet: Pet} -> Pet, ...": the keys and schemas,
        out |= set(_IDENT.findall(str(change.get("before") or ""))) | _WRAP_WORDS  # or saying it is (un)wrapped
    for name in list(out):  # a header written in words: "the request id header" for X-Request-Id
        if re.match(r"x-", name, re.I):
            out.add(name[2:])
    return out


_WRAP_WORDS = {"wrap", "wraps", "wrapped", "wrapping", "wrapper", "wrappers", "unwrap", "unwraps", "unwrapped",
               "unwrapping"}


def _same_thing(name, subject: str) -> bool:
    """A comparison's name and a subject name the same thing: Pet and Pet, GET /pets/{id} and GET /pets/{petId}."""
    a, b = _OPERATION.fullmatch(str(name or "")), _OPERATION.fullmatch(subject)
    return name == subject or bool(a and b and _operation(*a.groups()) == _operation(*b.groups()))


def _comparisons_about(ev: _Evidence, subject: str):
    owner, _, member = subject.rpartition(".") if subject.count(".") == 1 else ("", "", "")
    for comp in ev.comparisons:
        whole = _same_thing(comp.get("name"), subject)
        for change in comp.get("changes", []):
            things = _changed_thing(change)
            if whole and not change.get("where") or subject in things or \
                    owner and comp.get("name") == owner and member in things:  # `Owner.addresses` in Owner
                yield comp, change


def _is_path(name: str) -> bool:
    """A path through a schema, as a comparison writes where a change is: `pets[].tag`, `owner.address.city`."""
    return bool(_PATH_NAME.fullmatch(name)) and ("[" in name or name.count(".") > 1)


_PATH_NAME = re.compile(rf"{_PART}(?:\[\])*(?:\.{_PART}(?:\[\])*)+")
# "it was a string in 1.2", "used to be required in 1.2": the version where it still was, not where it went
_STILL = re.compile(r"\b(?:was|were|used to be|previously|formerly|originally|had been|existed)\b", re.I)
_GOING = re.compile(r"\b(?:removed|dropped|deleted|gone|replaced|renamed|no longer)\b", re.I)


def _removed_in(clause: str, labels: list[str], all_labels) -> list[str]:
    """The versions a removal claim says it is gone in. A version named as where it still was ("was an array
    in 1.2") stands for the one after it."""
    before = []
    for label in labels:
        at = [m.start() for m in re.finditer(rf"(?<![\w.]){re.escape(label)}(?![\w]|\.\d)", clause)]
        for m in _STILL.finditer(clause):
            if any(a > m.end() and not _GOING.search(clause[m.end():a]) and not re.search(r"[.;|]", clause[m.end():a])
                   for a in at):
                before.append(label)
                break
    if not before:
        return labels
    rest = [l for l in labels if l not in before]
    if rest:
        return rest
    later = sorted((l for l in all_labels if label_key(l) > label_key(max(before, key=label_key))), key=label_key)
    return later[:1]


def _at_path(change: dict, path: str) -> bool:
    where = re.sub(r"^option\s+", "", str(change.get("where") or "").split(": ")[-1])
    return where == path or where.endswith("." + path)


def _check_path(kind: str, clause: str, path: str, labels: list[str], ordered: list[str], ev: _Evidence,
                unsupported: list) -> list[Finding]:
    """A claim about a path, against the comparisons' changes at that path: there in a version (changed in it, or
    added by then), gone from it (removed by then), added in it."""
    out = []
    at = [(c, ch) for c in ev.comparisons for ch in c.get("changes", []) if isinstance(ch, dict) and _at_path(ch, path)]

    def removal(ch):
        return any(k in ch.get("kind", "") for k in _REMOVED_KINDS)

    def addition(ch):
        return any(k in ch.get("kind", "") for k in _ADDED_KINDS) and "required" not in ch.get("kind", "")

    def there(label):  # the evidence shows the path in that version
        return any(label == c["b_label"] and not removal(ch) or label == c["a_label"] and not addition(ch)
                   for c, ch in at)

    for label in labels:
        if kind in ("missing", "removed"):
            if there(label):
                out.append(Finding("contradicted-claim", clause, f"the evidence shows {path} in {label}", [label]))
            elif not any(c["b_label"] == label and removal(ch) or c["a_label"] == label and addition(ch)
                         for c, ch in at):
                unsupported.append(("nothing in the evidence shows", path, kind, label))
        elif kind == "added":
            earlier = [l for l in ordered if label_key(l) < label_key(label) and there(l)]
            if earlier:
                out.append(Finding("contradicted-claim", clause, f"the evidence shows {path} already in {earlier[0]}",
                                   earlier))
            elif not any(c["b_label"] == label and addition(ch) for c, ch in at):
                unsupported.append(("nothing in the evidence shows", path, "added", label))
        elif not any(label in (c["a_label"], c["b_label"]) for c, _ in at):
            unsupported.append(("no comparison in the evidence shows", path, "changed", label))
    return out


# What may be added inside a field or schema, rather than the thing itself: an enum value, a limit, a pattern
_INNER = (r"(?:enum(?:eration)?(?:\s+values?)?|allowed\s+values|possible\s+values|list\s+of\s+values|values?|"
         r"min(?:imum)?(?:\s+(?:length|value|items))?|max(?:imum)?(?:\s+(?:length|value|items))?|minLength|"
         r"maxLength|minItems|maxItems|minProperties|maxProperties|exclusiveMinimum|exclusiveMaximum|multipleOf|"
         r"(?:length\s+)?(?:limits?|constraints?|bounds?)|range|pattern|format|default(?:\s+value)?)")
_INSIDE = (
    # "added to the `x` enum", "added to `x`'s allowed values", "added to the allowed values of `x`"
    re.compile(rf"\badded\s+(?:to|into)\s+(?:the\s+)?(?:(?P<p1>{_INNER})\s+(?:of|for|on)\s+(?:the\s+)?"
               rf"`(?P<n1>[^`]+)`|`(?P<n2>[^`]+)`(?:'s)?\s+(?P<p2>{_INNER})\b)", re.IGNORECASE),
    # "a minimum of 5 was added to `x`", "a `minimum` constraint was added on `x`"
    re.compile(rf"\b(?P<p3>{_INNER})\b(?P<gap>[^`.;]{{0,40}}?)\s*`?\s*(?:was|were|is|are|has\s+been|have\s+been)?"
               rf"\s*added\s+(?:to|on|for)\s+(?:the\s+)?`(?P<n3>[^`]+)`", re.IGNORECASE),
    # "`x` now has a minimum", "`x` in `Y` now has both a minimum and a maximum", "`x` gained a pattern",
    # "`x` added the enum value `V`"
    re.compile(rf"(?:`(?P<n4>[^`]+)`(?:\s+(?:in|of|on)\s+(?:the\s+)?`[^`]+`)?(?:\s+(?:field|property|parameter))?"
               rf"\s+|\b(?:it|the\s+(?:field|property|parameter|value))\s+)(?:now\s+(?:has|have|carries)|gained|"
               rf"gains|gets|got|(?:has\s+)?added|adds)\s+(?:both\s+)?(?:(?:an?|new|the)\s+)*`?(?P<p4>{_INNER})\b`?"
               r"(?!\s*(?:field|property|parameter|header|attribute|option|operation|schema)s?\b)", re.IGNORECASE),
)
_NOT_A_PART = re.compile(r"\b(?:field|property|parameter|header|attribute|option|operation|schema|endpoint)s?\b",
                         re.IGNORECASE)
_PART_CHANGES = {"enum": ("enum-value-added", "const-changed"),
                 "limit": ("constraint-tightened", "constraint-loosened"),
                 "pattern": ("pattern-added", "pattern-changed"), "format": ("format-added", "format-changed"),
                 "default": ("default-changed",)}


def _added_inside(clause: str) -> tuple[str, str | None] | None:
    """(what kind of part, the name it is in) when a clause says something was added inside a field or schema:
    an enum value, a limit, a pattern, a format or a default. The name is None when the clause does not give
    one ("The field now has a minimum")."""
    for rx in _INSIDE:
        m = rx.search(clause)
        if not m or (m.groupdict().get("gap") and _NOT_A_PART.search(m.group("gap"))):
            continue
        part = next(m.group(k) for k in ("p1", "p2", "p3", "p4") if m.groupdict().get(k)).lower()
        name = next((m.group(k) for k in ("n1", "n2", "n3", "n4") if m.groupdict().get(k)), None)
        family = ("enum" if re.match(r"enum|allowed|possible|list|value", part) else
                  "pattern" if part == "pattern" else "format" if part == "format" else
                  "default" if part.startswith("default") else "limit")
        return family, (_path_name(name) or None) if name else None
    return None


def _said_value(change: dict, clause: str) -> bool:
    """The value a change gives ('missing', "maximum 720") is written in the clause."""
    value = str(change.get("after") or "").strip("'\"")
    value = value.split()[-1] if value else ""
    return bool(value) and value != "none" and re.search(rf"(?<![\w.-]){re.escape(value)}(?![\w-]|\.\d)", clause) is not None


def _enum_values_said(clause: str, name: str, containers: list[str], ev: _Evidence) -> set[str]:
    """The values a clause about an enum gives in code: `lost`, `'lost'`; not the names of fields, schemas or
    versions (in "`kind` and `category` gained new values", category is another field). What the clause puts
    where a value goes is a value even when a field has that name: "`tags` was added to the `x` enum", "`x`
    added the enum value `tags`"."""
    fields = set(ev.types) | {m for ms in ev.members.values() for _, m in ms} | {
        f for fs in ev.field_lists.values() for f in fs}
    placed = {m.group(1) for rx in _VALUE_PLACES for m in rx.finditer(clause)}
    out = set()
    for code in re.findall(r"`([^`]+)`", clause):
        v = code.strip().strip("'\"")
        if v and v != name and v not in containers and v not in ev.schemas and v not in ev.labels \
                and _path_name(code) != name and (v not in fields or code.strip()[:1] in "'\"" or code in placed):
            out.add(v)
    return out


_VALUE_PLACES = (
    re.compile(r"`([^`]+)`\s+(?:(?:was|were|is|are|has\s+been|have\s+been)\s+)?added\s+(?:to|into)\b", re.IGNORECASE),
    re.compile(r"\b(?:enum\s+)?values?\s+`([^`]+)`", re.IGNORECASE),
)


def _typed_as(types: set[str], change: dict) -> bool:
    """One of a field's shown types is the named schema a change is at: `Status`, `array of Status`."""
    schema = str(change.get("b") or change.get("a")).split("schema/", 1)[1]
    return any(schema in _IDENT.findall(t) for t in types)


def _check_inside(clause: str, family: str, name: str, containers: list[str], labels: list[str],
                  ev: _Evidence) -> list[Finding]:
    """A claim that something was added inside `name` (a value to its enum, a limit, a pattern): backed by a
    comparison showing that kind of change in that version. Not checked when no comparison covers it; said to be
    unsupported when one does and shows no such change."""
    out = []
    for label in labels:
        changes = [(c, ch) for c, ch in _comparisons_about(ev, name) if c.get("b_label") == label and (
            not containers or c.get("name") in containers or str(c.get("name", "")).split(".")[0] in containers
            or any(x in str(ch.get("a") or ch.get("b")) for x in containers))]
        # The change is about `name` itself, not an enum value that happens to be called that.
        changes = [(c, ch) for c, ch in changes if name not in _values_of(ch)]
        values = _enum_values_said(clause, name, containers, ev) if family == "enum" else set()
        if values and values <= {v for _, ch in changes if ch.get("kind") in _PART_CHANGES[family]
                                 for v in _values_of(ch)}:
            continue  # "`lost` was added to the `status` enum": lost is a value added to status
        if not values and any(ch.get("kind") in _PART_CHANGES[family] for _, ch in changes):
            continue
        # Comparisons of a schema that holds `name`: its own, its containers', or one whose field list has it
        covering = [c for c in ev.comparisons if c.get("b_label") == label and (
            _same_thing(c.get("name"), name) or str(c.get("name", "")).endswith("." + name)
            or c.get("name") in containers or name in ev.field_lists.get((label, c.get("name")), ())
            or (c.get("name"), name) in ev.members.get(label, set()))]
        # A change to a schema two fields share is listed once, at the first of them: a change of that kind to a
        # named schema, with the value the claim gives, backs it too, unless the rows shown for `name` give it
        # a type that is not that schema.
        types = ev.types.get(name, set())
        if any(ch.get("kind") in _PART_CHANGES[family] and "schema/" in str(ch.get("b") or ch.get("a"))
               and _said_value(ch, clause) and (not types or _typed_as(types, ch))
               for c in covering for ch in c.get("changes") or []):
            continue
        covered = changes or covering
        if covered:
            what = {"enum": "an enum value", "limit": "a limit", "pattern": "a pattern", "format": "a format",
                    "default": "a default"}[family]
            out.append(Finding("unsupported-claim", clause,
                               f"nothing in the evidence shows {what} added to {name} in {label}"))
    return out


def _check_claim(kind: str, clause: str, subjects: list[str], containers: list[str], labels: list[str],
                 ev: _Evidence) -> list[Finding]:
    out, unsupported = [], []
    ordered = sorted(ev.labels, key=label_key)
    where = f" in {', '.join(containers)}" if containers else ""
    for subject in subjects:
        if _is_path(subject):
            out += _check_path(kind, clause, subject, labels, ordered, ev, unsupported)
            continue
        for label in labels:
            shown_here = ev.present(label, subject, containers)
            earlier = [l for l in ordered if label_key(l) < label_key(label) and ev.present(l, subject, containers)]
            changes = [(c, ch) for c, ch in _comparisons_about(ev, subject)
                       if not containers or c.get("name") in containers or any(x in str(ch.get("a") or ch.get("b"))
                                                                               for x in containers)]
            if kind in ("missing", "removed"):
                if shown_here:
                    out.append(Finding("contradicted-claim", clause,
                                       f"the evidence shows {subject}{where} in {label}", [label]))
                    continue
                supported = shown_here is False or any(
                    c["b_label"] == label and any(k in ch["kind"] for k in _REMOVED_KINDS)
                    or c["a_label"] == label and any(k in ch["kind"] for k in _ADDED_KINDS) for c, ch in changes)
                if not supported:
                    unsupported.append(("nothing in the evidence shows", subject, kind, label))
            elif kind == "added":
                if earlier:
                    out.append(Finding("contradicted-claim", clause,
                                       f"the evidence shows {subject}{where} already in {earlier[0]}", earlier))
                    continue
                before = [l for l in ordered if label_key(l) < label_key(label)]
                # Backed by a comparison, or by complete field lists: there now, absent before.
                lists = shown_here and any(ev.present(l, subject, containers) is False for l in before)
                if not lists and not any(c["b_label"] == label and any(k in ch["kind"] for k in _ADDED_KINDS)
                                         for c, ch in changes):
                    unsupported.append(("nothing in the evidence shows", subject, "added", label))
            elif not any(label in (c["a_label"], c["b_label"]) for c, _ in changes) and not any(
                    label in (c["a_label"], c["b_label"]) and c.get("changes") and _same_thing(c.get("name"), subject)
                    for c in ev.comparisons):  # a comparison of the thing itself, with changes: "Pet changed"
                unsupported.append(("no comparison in the evidence shows", subject, "changed", label))
    # One claim is one finding, whatever number of names and versions it is about.
    for lead, verb in dict.fromkeys((lead, verb) for lead, _, verb, _ in unsupported):
        names = list(dict.fromkeys(x for l, x, v, _ in unsupported if (l, v) == (lead, verb)))
        labels_ = sorted(dict.fromkeys(x for l, _, v, x in unsupported if (l, v) == (lead, verb)), key=label_key)
        out.append(Finding("unsupported-claim", clause,
                           f"{lead} {', '.join(names)} {verb} in {', '.join(labels_)}"))
    return out

