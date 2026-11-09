"""The graph of one spec: operations, schemas, fields and parameters, and the edges between them.

Node ids are `<label>:<kind>/<name>`. Every node also has a `location`, `<file>#<json pointer>`, which is
always unique and says exactly where it was written.
"""

from collections import defaultdict
from dataclasses import dataclass, field

OPERATION = "operation"
SCHEMA = "schema"
FIELD = "field"
PARAMETER = "parameter"

# Edge kinds. Each is stored once, from -> to; `Graph.incoming` gives the reverse ("used by").
INHERITS = "inherits"  # schema -> schema: an allOf branch, or (3.1) a $ref beside other keywords
OPTION = "option"  # schema -> schema: oneOf / anyOf
CONDITIONAL = "conditional"  # schema -> schema: if / then / else, dependentSchemas
NOT = "not"  # schema -> schema
ITEMS = "items"  # schema -> schema: items, prefixItems, contains
VALUES = "values"  # schema -> schema: additionalProperties, patternProperties, unevaluatedProperties, propertyNames
FIELD_TYPE = "field_type"  # field -> schema
HAS_FIELD = "has_field"  # schema -> field, own or inherited
RETURNS = "returns"  # operation -> schema (a response)
ACCEPTS = "accepts"  # operation -> schema (the request body)
TAKES = "takes"  # operation -> parameter
CALLBACK = "callback"  # operation -> schema: the body of a request the API sends back (a callback)
PARAM_TYPE = "param_type"  # parameter -> schema


@dataclass
class Node:
    id: str
    label: str
    kind: str
    name: str
    location: str
    file: str
    line_start: int | None
    line_end: int | None
    data: dict = field(default_factory=dict)

    def where(self) -> str:
        if self.line_start is None:
            return self.file
        if self.line_end in (None, self.line_start):
            return f"{self.file}:{self.line_start}"
        return f"{self.file}:{self.line_start}-{self.line_end}"


@dataclass
class Edge:
    kind: str
    source: str  # node id
    target: str  # node id
    data: dict = field(default_factory=dict)


class Graph:
    def __init__(self, label: str):
        self.label = label
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self._out: dict[str, list[Edge]] = defaultdict(list)
        self._in: dict[str, list[Edge]] = defaultdict(list)
        self.by_location: dict[tuple[str, str], str] = {}  # (kind, location) -> id
        self.aliases: dict[str, list[str]] = {}  # an effective field address -> the field ids behind it
        self.security_schemes: dict[str, dict] = {}  # name -> type, in, name, scheme, flows, scopes, file, line

    def add_node(self, node: Node):
        self.nodes[node.id] = node
        self.by_location[(node.kind, node.location)] = node.id

    def add_edge(self, edge: Edge):
        self.edges.append(edge)
        self._out[edge.source].append(edge)
        self._in[edge.target].append(edge)

    def outgoing(self, node_id: str, kind: str | None = None) -> list[Edge]:
        return [e for e in self._out.get(node_id, ()) if kind is None or e.kind == kind]

    def incoming(self, node_id: str, kind: str | None = None) -> list[Edge]:
        return [e for e in self._in.get(node_id, ()) if kind is None or e.kind == kind]

    def of_kind(self, kind: str) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == kind]

    def resolve(self, node_id: str) -> list[str]:
        """The node ids an id names. An effective field address (`field/Dog.weight`) gives every
        field behind it, which can be more than one when several allOf branches define the same property."""
        if node_id in self.aliases:
            return list(self.aliases[node_id])
        return [node_id] if node_id in self.nodes else []
