"""Values of the wrong JSON type: a warning where they are written, and the rest of the spec read as usual."""

import copy
import json

import yaml
from conftest import codes

from speclens import SpecSet, load_spec
from speclens.descriptions import description_issues

SPEC = yaml.safe_load("""
openapi: 3.1.0
info: {title: t, version: '1'}
security: [{key: []}, {oauth: [read]}]
tags: [{name: pets}]
paths:
  /pets/{petId}:
    parameters:
      - {name: petId, in: path, required: true, schema: {type: string, maxLength: 10}}
    get:
      operationId: getPet
      summary: Get a pet
      tags: [pets]
      parameters:
        - {name: limit, in: query, schema: {type: integer, minimum: 1, maximum: 50}, description: at most 50}
      responses:
        '200':
          description: ok
          headers: {X-Rate: {schema: {type: integer}}}
          content: {application/json: {schema: {$ref: '#/components/schemas/Pet'}}}
          links: {self: {operationId: getPet, parameters: {petId: $response.body#/id}}}
        default: {$ref: '#/components/responses/Error'}
    put:
      operationId: putPet
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/Pet'}}}}
      callbacks: {done: {'{$request.body#/url}': {post: {requestBody: {content: {application/json: {schema: {$ref: '#/components/schemas/Pet'}}}}, responses: {'200': {description: ok}}}}}}
      responses: {'204': {description: none}}
components:
  securitySchemes:
    key: {type: apiKey, in: header, name: X-Key}
    oauth: {type: oauth2, flows: {implicit: {authorizationUrl: 'https://x', scopes: {read: r}}}}
  responses: {Error: {description: err, content: {application/json: {schema: {$ref: '#/components/schemas/Error'}}}}}
  schemas:
    Error:
      $schema: 'https://json-schema.org/draft/2020-12/schema'
      type: object
      required: [code]
      properties: {code: {type: integer}, message: {type: [string, 'null']}}
    Animal: {type: object, discriminator: {propertyName: kind, mapping: {pet: '#/components/schemas/Pet'}}, required: [kind], properties: {kind: {type: string, enum: [pet, wild]}}}
    Pet:
      allOf:
        - {$ref: '#/components/schemas/Animal'}
        - type: object
          required: [name]
          dependentRequired: {name: [kind]}
          properties:
            name: {type: string, maxLength: 40, description: The name, at most 40 characters., default: rex}
            tags: {type: array, minItems: 1, uniqueItems: true, items: {type: string}}
            owner: {oneOf: [{$ref: '#/components/schemas/Error'}, {type: 'null'}]}
            extra: {type: object, additionalProperties: {type: string}, patternProperties: {'^x-': {type: integer}}}
""")
JUNK = [True, None, 5, 1.5, "text", "", [], {}, ["a"], [1], {"a": 1}, {"$ref": "#/nowhere"}, {"$ref": 5},
        [{"$ref": "#/components/schemas/Pet"}], [[]]]


def _paths(value, at=()):
    yield at
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _paths(v, at + (k,))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _paths(v, at + (i,))


def _use(s, base):
    """Everything a client of speclens can ask of a loaded spec."""
    assert s.index is not None
    [i.to_text() for i in s.issues + description_issues(s)]
    s.list_operations().to_text()
    for node in list(s.graph.nodes.values()):
        if node.kind == "schema" and node.data.get("named"):
            r = s.get_schema(node.id, expand=1)
            r.to_text(True), r.to_dict()
        elif node.kind == "operation":
            r = s.get_operation(node.id, expand=1, errors=True)
            r.to_text(True), r.to_dict()
        elif node.kind == "field":
            s.find_field(node.id).to_text()
        s.source_of(node.id)
    s.search("pet name at most 40").to_text()
    s.names_in("What does GET /pets/{petId} return, the pet name and `limit`?").to_text()
    specs = SpecSet()
    specs.add(base)
    specs.add(s)
    specs.diff("base", "x").to_text(True)
    specs.diff("x", "base").to_text(True)


def test_a_value_of_the_wrong_type_is_a_warning_and_the_rest_is_read(spec):
    s = spec({"openapi.yaml": """
        openapi: 3.0.3
        info: {title: t, version: '1'}
        paths:
          /p:
            get:
              tags: true
              summary: 2020
              parameters:
                - {name: [a], in: query, schema: {type: {}}}
              responses: {'200': {description: ok}}
        components:
          schemas:
            Pet:
              type: object
              required: true
              properties:
                n: {type: [string, 5], maxLength: ten}
                m: {type: string, maxLength: 3}
        """})
    assert [(i.code, i.line, i.message) for i in s.issues] == [
        ("invalid-value", 7, "`summary` should be text, found `2020`; read as its text"),
        ("invalid-value", 6, "`tags` should be a list of names, found `true`; ignored"),
        ("invalid-value", 9, "`name` should be text, found a list; ignored"),
        ("invalid-value", 9, "`type` should be a type name or a list of them, found a map; ignored"),
        ("invalid-value", 15, "`required` should be a list of names, found `true`; ignored"),
        ("invalid-value", 17, ("`type` should be a type name or a list of them, found a list holding other values; "
                               "ignored")),
        ("invalid-value", 17, "`maxLength` should be a number, found text; ignored")]
    assert all(i.severity == "warning" for i in s.issues)
    pet = s.get_schema("Pet")
    assert [(f.name, f.type, f.required) for f in pet.fields] == [("n", "any", False), ("m", "string", False)]
    assert pet.fields[1].limits == {"maxLength": 3}
    op = s.get_operation("GET /p")
    assert op.summary == "2020" and op.parameters[0].id == "v1:parameter/GET /p#parameters/0"


def test_no_single_value_of_the_wrong_type_stops_anything(tmp_path):
    """Each value of a small spec that uses most of OpenAPI, replaced by values of other types in turn: loading,
    every query and a diff both ways still work, and each problem is an issue, never an exception."""
    (tmp_path / "base.json").write_text(json.dumps(SPEC))
    base = load_spec(tmp_path / "base.json", "base")
    assert codes(base) == []
    places = [p for p in _paths(SPEC) if p]
    for n, place in enumerate(places):
        for junk in (JUNK[n % len(JUNK)], JUNK[(n + 5) % len(JUNK)], JUNK[(n + 10) % len(JUNK)]):
            d = copy.deepcopy(SPEC)
            holder = d
            for k in place[:-1]:
                holder = holder[k]
            holder[place[-1]] = copy.deepcopy(junk)
            (tmp_path / "x.json").write_text(json.dumps(d))
            try:
                _use(load_spec(tmp_path / "x.json", "x"), base)
            except Exception as e:  # which value, and what it raised
                raise AssertionError(f"{'/'.join(map(str, place))} = {junk!r}: {type(e).__name__}: {e}") from e
