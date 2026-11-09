"""Resolving `$ref`: local, across files, circular, broken, and what OpenAPI 3.0 and 3.1 each allow."""

from textwrap import dedent

from conftest import codes

HEAD = "openapi: 3.1.0\ninfo: {title: t, version: '1'}\n"
HEAD_30 = "openapi: 3.0.3\ninfo: {title: t, version: '1'}\n"


def test_local_and_cross_file_references(spec):
    s = spec({
        "openapi.yaml": HEAD + dedent("""
            paths:
              /pets:
                get:
                  responses:
                    '200':
                      content:
                        application/json:
                          schema: {$ref: './schemas/common.yaml#/components/schemas/Pet'}
            """),
        "schemas/common.yaml": """
            components:
              schemas:
                Pet:
                  properties:
                    tag: {$ref: '#/components/schemas/Tag'}
                    owner: {$ref: './owner.yaml'}
                Tag: {type: string}
            """,
        "schemas/owner.yaml": "type: object\nproperties:\n  name: {type: string}\n",
    })
    assert s.ok, s.issues
    g = s.graph
    returns = g.outgoing("v1:operation/GET /pets", "returns")
    assert [e.target for e in returns] == ["v1:schema/Pet"]
    assert g.nodes["v1:schema/Pet"].file == "schemas/common.yaml"
    types = {g.nodes[e.source].name: e.target for e in g.edges if e.kind == "field_type"}
    assert types == {"Pet.tag": "v1:schema/Tag", "Pet.owner": "v1:schema/owner"}
    assert len(s.documents) == 3


def test_escaped_and_percent_encoded_pointers(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        paths:
          /pets/{id}:
            get:
              responses:
                '200': {$ref: '#/components/responses/Ok%20Pet'}
            put:
              responses:
                '200': {$ref: '#/paths/~1pets~1{id}/get/responses/200'}
        components:
          responses:
            Ok Pet:
              description: ok
              content:
                application/json:
                  schema: {type: object, properties: {a: {type: string}}}
        """)})
    assert s.ok, s.issues
    for op in ("GET", "PUT"):
        assert [e.target for e in s.graph.outgoing(f"v1:operation/{op} /pets/{{id}}", "returns")] == \
            ["v1:schema/responses/Ok Pet#application/json"]


def test_one_file_reached_two_ways_is_loaded_once(spec):
    s = spec({
        "openapi.yaml": HEAD + dedent("""
            components:
              schemas:
                A: {$ref: './common.yaml#/components/schemas/X'}
                B: {$ref: './sub/../common.yaml#/components/schemas/X'}
            """),
        "common.yaml": "components:\n  schemas:\n    X: {type: object, properties: {p: {type: string}}}\n",
        "sub/.keep": "",
    })
    assert s.ok, s.issues
    assert len(s.documents) == 2
    targets = {e.source: e.target for e in s.graph.edges if e.kind == "inherits"}
    assert targets == {"v1:schema/A": "v1:schema/X", "v1:schema/B": "v1:schema/X"}


def test_circular_schemas_are_edges_not_endless(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            Node:
              properties:
                children: {type: array, items: {$ref: '#/components/schemas/Node'}}
                parent: {$ref: '#/components/schemas/Node'}
        """)})
    assert s.ok, s.issues
    g = s.graph
    assert [e.target for e in g.outgoing("v1:field/Node.parent", "field_type")] == ["v1:schema/Node"]
    assert [e.target for e in g.outgoing("v1:schema/Node.children", "items")] == ["v1:schema/Node"]


def test_references_that_only_point_to_each_other(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            A: {$ref: '#/components/schemas/B'}
            B: {$ref: '#/components/schemas/A'}
        """)})
    assert "circular-ref" in codes(s)


def test_broken_reference_names_the_file_and_line(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            A:
              properties:
                b: {$ref: '#/components/schemas/Missing'}
        """)})
    [issue] = s.errors
    assert (issue.code, issue.file, issue.line) == ("unresolved-ref", "openapi.yaml", 8)
    assert "'Missing' not found" in issue.message


def test_missing_file_remote_and_anchor_references(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            A: {$ref: './nowhere.yaml'}
            B: {$ref: 'https://example.com/schemas.yaml#/X'}
            C: {$ref: '#pet'}
        """)})
    assert sorted(codes(s)) == ["missing-file", "unresolved-ref", "unresolved-ref", "unsupported-ref"]
    assert "no anchor 'pet' in openapi.yaml" in str(s.issues)


def test_references_outside_the_graph_are_checked_too(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          links:
            L: {$ref: '#/components/links/Missing'}
          examples:
            E: {$ref: '#/components/examples/Missing'}
          schemas:
            S:
              example: {$ref: 'this is data, not a reference'}
              properties:
                default: {$ref: '#/components/schemas/Missing'}
        """)})
    assert sorted((i.code, i.line) for i in s.errors) == [("unresolved-ref", 6), ("unresolved-ref", 8),
                                                         ("unresolved-ref", 13)]


def test_openapi_30_ignores_keywords_beside_ref(spec):
    s = spec({"openapi.yaml": HEAD_30 + dedent("""
        components:
          schemas:
            A:
              properties:
                b:
                  $ref: '#/components/schemas/B'
                  description: kept, as the author's note for this field
                  nullable: true
            B: {type: object, properties: {x: {type: string}}}
        """)})
    assert s.version == "3.0"
    assert codes(s) == ["ignored-beside-ref"]
    g = s.graph
    assert [e.target for e in g.outgoing("v1:field/A.b", "field_type")] == ["v1:schema/B"]
    assert g.nodes["v1:field/A.b"].data["description"].startswith("kept")


def test_openapi_31_keywords_beside_ref_make_a_schema(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            A:
              properties:
                b:
                  $ref: '#/components/schemas/B'
                  properties: {extra: {type: string}}
                c:
                  $ref: '#/components/schemas/B'
                  description: only an annotation, so still B itself
            B: {type: object, properties: {x: {type: string}}}
        """)})
    assert s.ok and not s.issues, s.issues
    g = s.graph
    assert [e.target for e in g.outgoing("v1:field/A.b", "field_type")] == ["v1:schema/A.b"]
    assert [e.target for e in g.outgoing("v1:schema/A.b", "inherits")] == ["v1:schema/B"]
    assert g.resolve("v1:field/A.b.x") == ["v1:field/B.x"]
    assert [e.target for e in g.outgoing("v1:field/A.c", "field_type")] == ["v1:schema/B"]


def test_unsupported_versions_and_dialects(spec):
    assert codes(spec({"openapi.yaml": "swagger: '2.0'\ninfo: {title: t, version: '1'}\n"})) == \
        ["unsupported-version"]
    s = spec({"openapi.yaml": HEAD + "jsonSchemaDialect: https://example.com/my-dialect\n"})
    assert codes(s) == ["unknown-dialect"]
    s = spec({"openapi.yaml": HEAD + "jsonSchemaDialect: https://spec.openapis.org/oas/3.1/dialect/base\n"})
    assert s.ok


def test_operation_kept_in_its_own_file_is_followed_and_reported(spec):
    # OpenAPI does not allow it, but specs split into many files often do this.
    s = spec({
        "openapi.yaml": HEAD + "paths:\n  /pets:\n    get:\n      $ref: 'ops/list_pets.yaml'\n",
        "ops/list_pets.yaml": """
            operationId: listPets
            parameters:
              - {name: limit, in: query, schema: {type: integer}}
            responses:
              '200':
                content:
                  application/json:
                    schema: {type: array, items: {type: string}}
            """,
    })
    assert s.ok and codes(s) == ["nonstandard-ref"]
    g = s.graph
    op = g.nodes["v1:operation/GET /pets"]
    assert (op.file, op.data["operationId"]) == ("ops/list_pets.yaml", "listPets")
    # What sits inside it is named by the operation, not by the file.
    assert [e.target for e in g.outgoing(op.id, "takes")] == ["v1:parameter/GET /pets#query/limit"]
    assert [e.target for e in g.outgoing(op.id, "returns")] == ["v1:schema/GET /pets#response/200/application/json"]


def test_files_holding_one_schema_or_a_collection(spec):
    s = spec({
        "openapi.yaml": HEAD + dedent("""
            components:
              schemas:
                A:
                  properties:
                    pet: {$ref: './pet.yaml'}
                    tag: {$ref: './shared/definitions.yaml#/Tag'}
                    other: {$ref: './more/definitions.yaml#/Tag'}
                    size: {$ref: './shared/definitions.yaml#/Size'}
            """),
        "pet.yaml": "type: object\nproperties:\n  name: {type: string}\n",
        "shared/definitions.yaml": "Tag: {properties: {label: {type: string}}}\nSize: {properties: {n: {type: integer}}}\n",
        "more/definitions.yaml": "Tag: {properties: {code: {type: string}}}\n",
    })
    assert s.ok, s.issues
    ids = set(s.graph.nodes)
    assert {"v1:schema/pet", "v1:field/pet.name", "v1:schema/Size", "v1:field/Size.n"} <= ids
    # Two files each define Tag, so both are named by location.
    assert {"v1:schema/shared/definitions.yaml#/Tag", "v1:schema/more/definitions.yaml#/Tag"} <= ids


def test_a_simple_inline_type_referred_to_elsewhere_is_linked(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        paths:
          /jobs:
            get:
              parameters:
                - {name: name, in: query, schema: {type: string, pattern: '^[a-z]+$'}}
              responses:
                '200':
                  content:
                    application/json:
                      schema: {$ref: '#/components/schemas/Job'}
        components:
          schemas:
            Job:
              properties:
                version: {type: string}
            Names: {type: array, items: {$ref: '#/paths/~1jobs/get/parameters/0/schema'}}
            Versions: {type: array, items: {$ref: '#/components/schemas/Job/properties/version'}}
        """)})
    assert s.ok, s.issues
    g = s.graph
    param_schema = "v1:schema/GET /jobs#query/name"
    assert [e.target for e in g.outgoing("v1:parameter/GET /jobs#query/name", "param_type")] == [param_schema]
    assert [u["operation"] for u in s.index.used_by(param_schema)] == ["v1:operation/GET /jobs"]
    assert [e.target for e in g.outgoing("v1:field/Job.version", "field_type")] == ["v1:schema/Job.version"]
    assert [u["operation"] for u in s.index.used_by("v1:schema/Job.version")] == ["v1:operation/GET /jobs"]


def test_schema_examples_must_be_a_list_in_31(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            A:
              examples: {first: {a: 1}}
              properties:
                b: {type: string, examples: {x: 1}}
                c: {type: string, examples: [ok]}
        """)})
    assert [(i.code, i.line) for i in s.issues] == [("invalid-keyword", 7), ("invalid-keyword", 9)]
    assert s.ok  # read anyway; other tools may refuse it


def test_a_component_header_is_not_a_named_schema(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          headers:
            X-Rate: {schema: {type: integer}}
          schemas:
            Pet: {type: object}
        """)})
    named = [n.name for n in s.graph.of_kind("schema") if n.data["named"]]
    assert named == ["Pet"]


def test_a_component_kept_in_another_file_is_named_as_the_component(spec):
    s = spec({
        "openapi.yaml": HEAD_30 + dedent("""
            paths:
              /pets:
                get:
                  parameters:
                    - $ref: './parameters.yaml#/limit'
                  responses:
                    '200':
                      description: ok
                      headers: {X-Rate: {$ref: './headers.yaml#/X-Rate'}}
                      content: {application/json: {schema: {$ref: '#/components/schemas/Pet'}}}
            components:
              schemas:
                Pet: {$ref: './schemas.yaml#/Pet'}
                Owner: {$ref: './schemas/Owner.yaml'}
                Companion: {$ref: './schemas.yaml#/Pet'}
              parameters:
                limit: {$ref: './parameters.yaml#/limit'}
              headers:
                X-Rate: {$ref: './headers.yaml#/X-Rate'}
            """),
        "schemas.yaml": dedent("""
            Pet:
              required: [name]
              properties:
                name: {type: string}
                owner: {$ref: './schemas/Owner.yaml'}
            """),
        "schemas/Owner.yaml": "properties: {nickname: {type: string, maxLength: 40}}\n",
        "parameters.yaml": "limit: {name: limit, in: query, schema: {type: integer, maximum: 50}}\n",
        "headers.yaml": "X-Rate: {schema: {type: integer}}\n",
    })
    assert s.ok, s.issues
    named = sorted(n.name for n in s.graph.of_kind("schema") if n.data.get("named"))
    # One Pet and one Owner, under their component names; an alias with a name of its own stays.
    assert named == ["Companion", "Owner", "Pet"]
    assert [f.name for f in s.get_schema("Pet").fields] == ["name", "owner"]
    assert s.get_schema("Owner").fields[0].name == "nickname"
    assert "v1:field/Pet.name" in s.graph.nodes
    # A file of parameters or headers holds parameters and headers, not schemas.
    assert "v1:parameter/limit" in s.graph.nodes
