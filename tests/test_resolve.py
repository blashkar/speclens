"""Resolving `$ref`: local, across files, circular, broken, and what OpenAPI 3.0, 3.1 and 3.2 each allow."""

from textwrap import dedent

from conftest import codes

from speclens import SpecSet

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


def test_openapi_3_2_is_read_as_3_1_with_the_query_method(spec):
    paths = """
        paths:
          /pets:
            get:
              responses: {'200': {description: ok}}
            query:
              operationId: findPets
              requestBody:
                content:
                  application/json:
                    schema: {$ref: '#/components/schemas/Filter'}
              responses: {'200': {description: ok}}
        components:
          schemas:
            Filter: {type: object, properties: {name: {type: string, examples: one}}}
        """
    s = spec({"openapi.yaml": "openapi: 3.2.0\ninfo: {title: t, version: '1'}\n" + dedent(paths)})
    assert s.version == "3.2"
    # Schemas follow 3.1's rules: `examples` must be an array.
    assert codes(s) == ["invalid-keyword"]
    assert sorted(n.id for n in s.graph.of_kind("operation")) == [
        "v1:operation/GET /pets", "v1:operation/QUERY /pets"]
    op = s.get_operation("QUERY /pets")
    assert (op.id, op.method) == ("v1:operation/QUERY /pets", "QUERY")
    assert "request body (optional): application/json: Filter [v1:schema/Filter]" in op.to_text()
    assert [m.ids for m in s.names_in("What does QUERY /pets take?").mentions] == [["v1:operation/QUERY /pets"]]
    # In 3.1, `query` in a path item is not an operation: it is ignored, and said once.
    s = spec({"openapi.yaml": HEAD + dedent(paths)})
    assert [n.id for n in s.graph.of_kind("operation")] == ["v1:operation/GET /pets"]
    assert [(i.code, i.message, i.line) for i in s.issues if i.code != "invalid-keyword"] == [
        ("unsupported-keyword", "`query` is an OpenAPI 3.2 operation; this spec is 3.1, so it is ignored", 8)]
    # The minor version whole: 3.20 is not 3.2, nor 3.10 3.1.
    for version in ("3.3.0", "3.20.0", "3.10.0", "3.2x"):
        root = f"openapi: {version}\ninfo: {{title: t, version: '1'}}\npaths: {{}}\n"
        assert codes(spec({"openapi.yaml": root})) == ["unsupported-version"], version
    for version in ("3.2", "3.2.1"):
        s = spec({"openapi.yaml": f"openapi: {version}\ninfo: {{title: t, version: '1'}}\npaths: {{}}\n"})
        assert (s.version, codes(s)) == ("3.2", []), version


def test_openapi_3_2_additional_operations_and_item_schemas(spec, write):
    paths = """
        paths:
          /pets/{id}:
            parameters:
              - {name: id, in: path, required: true, schema: {type: string}}
            get:
              responses: {'200': {description: ok}}
            additionalOperations:
              COPY:
                operationId: copyPet
                responses: {'201': {description: copied}}
              POST: {responses: {'200': {description: ok}}}
              not a method: {responses: {'200': {description: ok}}}
          /events:
            get:
              responses:
                '200':
                  description: a stream
                  content:
                    application/jsonl:
                      schema: {type: array, items: {$ref: '#/components/schemas/Event'}}
                      itemSchema: {type: object, properties: {kind: {type: string%s}}}
        components:
          schemas:
            Event: {type: object}
        """
    v32 = "openapi: 3.2.0\ninfo: {title: t, version: '1'}\n"
    s = spec({"openapi.yaml": v32 + dedent(paths % "")})
    # A method as sent (COPY) is an operation; one that a fixed field names (POST), or not a method, is not.
    assert [(i.code, i.message) for i in s.issues] == [
        ("invalid-value", "`additionalOperations` cannot hold 'POST' (use `post`); ignored"),
        ("invalid-value", "`additionalOperations` cannot hold 'not a method' (not a method name); ignored")]
    assert sorted(n.id for n in s.graph.of_kind("operation")) == [
        "v1:operation/COPY /pets/{id}", "v1:operation/GET /events", "v1:operation/GET /pets/{id}"]
    op = s.get_operation("COPY /pets/{id}")
    assert (op.method, op.operation_id, [p.name for p in op.parameters]) == ("COPY", "copyPet", ["id"])
    assert [m.ids for m in s.names_in("What does COPY /pets/{id} return?").mentions] == [
        ["v1:operation/COPY /pets/{id}"]]
    # itemSchema is the schema of each item of a stream, beside the schema of the whole.
    text = s.get_operation("GET /events").to_text()
    assert "  200 application/jsonl: array of Event [v1:schema/GET /events#response/200/application/jsonl]" in text
    assert "  200 application/jsonl (each item): object " \
        "[v1:schema/GET /events#response/200/application/jsonl#itemSchema]" in text
    root = write({"a/openapi.yaml": v32 + dedent(paths % ""),
                  "b/openapi.yaml": v32 + dedent(paths % ", maxLength: 9").replace("copyPet", "clonePet")})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    assert [(c.kind, c.where) for c in s.compare("GET /events", "a", "b").changes] == [
        ("constraint-tightened", "response 200 application/jsonl (each item): kind")]
    assert [c.kind for c in s.compare("COPY /pets/{id}", "a", "b").changes] == ["operation-id-changed"]
    # Before 3.2 neither exists: ignored, and said.
    s = spec({"openapi.yaml": HEAD + dedent(paths % "")})
    assert sorted(n.id for n in s.graph.of_kind("operation")) == ["v1:operation/GET /events",
                                                                    "v1:operation/GET /pets/{id}"]
    assert [i.message for i in s.issues] == [
        "`additionalOperations` is OpenAPI 3.2; this spec is 3.1, so it is ignored",
        "`itemSchema` is OpenAPI 3.2; this spec is 3.1, so it is ignored"]


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
