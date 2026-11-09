"""The graph: node ids, operations and parameters, field identity and inheritance."""

from textwrap import dedent

import pytest

from conftest import codes
from speclens import SpecSet, load_spec

HEAD = "openapi: 3.1.0\ninfo: {title: t, version: '1'}\n"

PETS = HEAD + dedent("""
paths:
  /pets/{petId}:
    parameters:
      - {$ref: '#/components/parameters/PetId'}
      - {name: limit, in: query, schema: {type: integer}}
    get:
      parameters:
        - {name: limit, in: query, description: the operation's own, schema: {type: integer, maximum: 10}}
        - {name: limit, in: header, schema: {type: string}}
      responses:
        '200':
          content:
            application/json:
              schema: {$ref: '#/components/schemas/Dog'}
        '404':
          content:
            application/json:
              schema:
                type: object
                properties:
                  message: {type: string}
    put:
      requestBody:
        required: true
        content:
          application/json:
            schema: {$ref: '#/components/schemas/Dog'}
      responses:
        '204': {description: done}
components:
  parameters:
    PetId: {name: petId, in: path, required: true, schema: {type: string}}
  schemas:
    Pet:
      type: object
      required: [petId]
      properties:
        petId: {type: string}
        weight: {type: number}
    Dog:
      allOf:
        - $ref: '#/components/schemas/Pet'
        - type: object
          required: [weight]
          properties:
            speed: {type: number}
""")


@pytest.fixture
def pets(spec):
    s = spec({"openapi.yaml": PETS})
    assert s.ok and not s.issues, s.issues
    return s


def test_operations_and_merged_parameters(pets):
    g = pets.graph
    op = "v1:operation/GET /pets/{petId}"
    assert g.nodes[op].data["method"] == "GET"
    takes = {(e.data["in"], e.data["name"]): (e.target, e.data["level"]) for e in g.outgoing(op, "takes")}
    assert takes == {
        ("path", "petId"): ("v1:parameter/PetId", "path"),
        ("query", "limit"): ("v1:parameter/GET /pets/{petId}#query/limit", "operation"),
        ("header", "limit"): ("v1:parameter/GET /pets/{petId}#header/limit", "operation"),
    }
    assert g.nodes["v1:parameter/GET /pets/{petId}#query/limit"].data["maximum"] == 10
    assert "v1:parameter//pets/{petId}#query/limit" in g.nodes  # the path-level one, overridden


def test_responses_and_request_bodies(pets):
    g = pets.graph
    returns = [(e.data["status"], e.target) for e in g.outgoing("v1:operation/GET /pets/{petId}", "returns")]
    assert returns == [("200", "v1:schema/Dog"),
                       ("404", "v1:schema/GET /pets/{petId}#response/404/application/json")]
    [accepts] = g.outgoing("v1:operation/PUT /pets/{petId}", "accepts")
    assert (accepts.target, accepts.data["required"]) == ("v1:schema/Dog", True)
    used_by = {e.source for e in g.incoming("v1:schema/Dog")}
    assert used_by == {"v1:operation/GET /pets/{petId}", "v1:operation/PUT /pets/{petId}"}


def test_a_field_exists_once_where_it_is_written(pets):
    g = pets.graph
    fields = sorted(n.id for n in g.of_kind("field"))
    assert fields == ["v1:field/Dog.speed", "v1:field/GET /pets/{petId}#response/404/application/json.message",
                      "v1:field/Pet.petId", "v1:field/Pet.weight"]
    # The inline allOf branch belongs to Dog: its field is Dog's own.
    has = {e.target: e.data for e in g.outgoing("v1:schema/Dog", "has_field")}
    assert has["v1:field/Dog.speed"] == {"inherited": False, "via": [], "required": False}
    assert has["v1:field/Pet.weight"] == {"inherited": True, "via": ["v1:schema/Pet"], "required": True}
    assert has["v1:field/Pet.petId"]["required"] is True


def test_effective_address_resolves_to_where_the_field_is_written(pets):
    g = pets.graph
    assert g.resolve("v1:field/Dog.weight") == ["v1:field/Pet.weight"]
    assert g.resolve("v1:field/Pet.weight") == ["v1:field/Pet.weight"]
    assert g.resolve("v1:field/Dog.nothing") == []


def test_required_is_the_union_over_the_chain(pets):
    g = pets.graph
    assert g.nodes["v1:schema/Dog"].data["required"] == ["weight"]
    assert g.nodes["v1:schema/Dog"].data["effective_required"] == ["petId", "weight"]
    # In Pet alone, weight is not required.
    [e] = [e for e in g.outgoing("v1:schema/Pet", "has_field") if e.target.endswith(".weight")]
    assert e.data["required"] is False
    assert g.nodes["v1:field/Pet.weight"].data["required"] is False


def test_nodes_know_their_lines_and_source(pets):
    node = pets.graph.nodes["v1:schema/Pet"]
    assert (node.file, node.location) == ("openapi.yaml", "openapi.yaml#/components/schemas/Pet")
    assert pets.source(node.id).strip().startswith("Pet:")
    assert pets.source(node.id).strip().endswith("weight: {type: number}")
    assert node.where() == f"openapi.yaml:{node.line_start}-{node.line_end}"


def test_same_property_in_two_branches(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            Base: {properties: {amount: {type: integer}, code: {type: string}}}
            Both:
              allOf:
                - $ref: '#/components/schemas/Base'
                - properties: {amount: {type: number}, code: {type: integer}}
        """)})
    g = s.graph
    # integer and number overlap; string and integer do not.
    assert [i.code for i in s.issues] == ["contradiction"]
    assert "Both.code" in s.issues[0].message
    assert sorted(g.resolve("v1:field/Both.amount")) == ["v1:field/Base.amount", "v1:field/Both.amount"]


def test_same_property_in_two_inline_branches_keeps_the_branch_in_its_id(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            Repo:
              allOf:
                - properties: {name: {type: string}, url: {type: string}}
                - properties: {name: {type: string, maxLength: 10}}
        """)})
    assert s.ok and not s.issues, s.issues
    g = s.graph
    assert sorted(n.id for n in g.of_kind("field")) == ["v1:field/Repo#allOf/0.name", "v1:field/Repo#allOf/1.name",
                                                        "v1:field/Repo.url"]
    assert g.resolve("v1:field/Repo.name") == ["v1:field/Repo#allOf/0.name", "v1:field/Repo#allOf/1.name"]


def test_conditional_fields_are_not_all_fields(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            Pet:
              properties: {name: {type: string}}
              oneOf:
                - $ref: '#/components/schemas/Cat'
                - properties: {bark: {type: boolean}}
              discriminator:
                propertyName: kind
                mapping: {kitty: '#/components/schemas/Cat'}
              if: {properties: {name: {const: rex}}}
              then: {required: [bark]}
              not: {required: [both]}
              dependentSchemas:
                name: {properties: {nickname: {type: string}}}
            Cat: {properties: {meow: {type: boolean}}}
        """)})
    assert s.ok and not s.issues, s.issues
    g = s.graph
    assert [e.target for e in g.outgoing("v1:schema/Pet", "has_field")] == ["v1:field/Pet.name"]
    options = [(e.target, e.data["values"]) for e in g.outgoing("v1:schema/Pet", "option")]
    assert options == [("v1:schema/Cat", ["kitty"]), ("v1:schema/Pet#oneOf/1", [])]
    assert g.outgoing("v1:schema/Pet", "option")[0].data["discriminator"] == "kind"
    conditional = [(e.data["keyword"], e.target) for e in g.outgoing("v1:schema/Pet", "conditional")]
    assert conditional == [("if", "v1:schema/Pet#if"), ("then", "v1:schema/Pet#then"),
                           ("dependentSchemas", "v1:schema/Pet#dependentSchemas/name")]
    assert [e.target for e in g.outgoing("v1:schema/Pet", "not")] == ["v1:schema/Pet#not"]
    assert "v1:field/Pet#oneOf/1.bark" in g.nodes


def test_implicit_discriminator_values_are_component_names(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            Pet:
              oneOf: [{$ref: '#/components/schemas/Cat'}, {$ref: '#/components/schemas/Dog'}]
              discriminator: {propertyName: kind, mapping: {hound: Dog}}
            Cat: {type: object}
            Dog: {type: object}
        """)})
    assert [e.data["values"] for e in s.graph.outgoing("v1:schema/Pet", "option")] == [["Cat"], ["hound"]]


def test_a_name_defined_in_two_files_is_named_by_location(spec):
    s = spec({
        "openapi.yaml": HEAD + dedent("""
            components:
              schemas:
                Pet:
                  properties: {id: {type: string}, other: {$ref: './other.yaml#/components/schemas/Pet'}}
                Unique: {type: object}
            """),
        "other.yaml": "components:\n  schemas:\n    Pet:\n      properties: {number: {type: string}}\n",
    })
    ids = sorted(s.graph.nodes)
    assert "v1:schema/Unique" in ids
    assert "v1:schema/openapi.yaml#/components/schemas/Pet" in ids
    assert "v1:schema/other.yaml#/components/schemas/Pet" in ids
    # Their fields carry the qualified name too, so neither is mistaken for the other.
    assert "v1:field/other.yaml#/components/schemas/Pet.number" in ids
    assert not any(i.endswith("schema/Pet") or "field/Pet." in i for i in ids)


def test_ids_do_not_depend_on_the_order_files_are_read(write):
    common = "components:\n  schemas:\n    X: {properties: {a: {type: string}}}\n"
    other = "components:\n  schemas:\n    Y: {properties: {b: {$ref: './common.yaml#/components/schemas/X'}}}\n"
    first = write({"one/openapi.yaml": HEAD + "components:\n  schemas:\n"
                   "    A: {$ref: './common.yaml#/components/schemas/X'}\n"
                   "    B: {$ref: './other.yaml#/components/schemas/Y'}\n",
                   "one/common.yaml": common, "one/other.yaml": other})
    second = write({"two/openapi.yaml": HEAD + "components:\n  schemas:\n"
                    "    B: {$ref: './other.yaml#/components/schemas/Y'}\n"
                    "    A: {$ref: './common.yaml#/components/schemas/X'}\n",
                    "two/common.yaml": common, "two/other.yaml": other})
    a = load_spec(first / "one/openapi.yaml", "v1").graph
    b = load_spec(second / "two/openapi.yaml", "v1").graph
    assert sorted(a.nodes) == sorted(b.nodes)


def test_labels(spec, tmp_path):
    s = spec({"openapi.yaml": HEAD})
    assert s.graph.label == "v1"
    assert load_spec(tmp_path / "openapi.yaml").label == "1"  # info.version
    with pytest.raises(ValueError):
        load_spec(tmp_path / "openapi.yaml", "a:b")
    specs = SpecSet()
    for label in ("2.10", "2.2", "11.3", "1.3"):
        specs.load(tmp_path / "openapi.yaml", label)
    assert specs.labels() == ["1.3", "2.2", "2.10", "11.3"]
    assert specs.get("1.3").label == "1.3"
    with pytest.raises(KeyError, match="loaded: 1.3, 2.2, 2.10, 11.3"):
        specs.get("1.30")
    with pytest.raises(ValueError):
        specs.load(tmp_path / "openapi.yaml", "2.2")


def test_webhooks_are_operations(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        webhooks:
          newPet:
            post:
              requestBody:
                content:
                  application/json:
                    schema: {type: object, properties: {id: {type: string}}}
        """)})
    assert "v1:operation/POST webhook:newPet" in s.graph.nodes
    assert "v1:schema/POST webhook:newPet#request/application/json" in s.graph.nodes
    assert codes(s) == []
