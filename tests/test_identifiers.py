"""JSON Schema 2020-12 identifiers in OpenAPI 3.1: `$id` base URIs, `$anchor`, `$dynamicRef`/`$dynamicAnchor`.

The suite tests run the schemas of the JSON-Schema-Test-Suite's reference files (see fixtures/README.md): each
`$ref` and `$dynamicRef` must land where the `referencing` library (the suite author's reference implementation)
lands, or be a remote reference, which speclens reports and does not fetch.
"""

import json
from pathlib import Path
from textwrap import dedent

import pytest

from speclens import graph as g
from speclens import pointer
from speclens.build import _Builder

from conftest import codes

SUITE = Path(__file__).parent / "fixtures" / "json-schema-test-suite"
FILES = ["ref.json", "anchor.json", "dynamicRef.json", "defs.json", "refRemote.json"]
REMOTE = ("http://localhost:1234/", "https://json-schema.org/")  # the suite's test server and the metaschema
HEAD = "openapi: 3.1.0\ninfo: {title: t, version: '1'}\n"


def type_of(s, field_id: str) -> str:
    """Where the schema a field holds is written."""
    return s.graph.nodes[s.graph.outgoing(field_id, g.FIELD_TYPE)[0].target].location


def suite_cases():
    for name in FILES:
        for i, group in enumerate(json.loads((SUITE / name).read_text())):
            yield pytest.param(group["schema"], id=f"{name[:-5]}-{i}")


def references(value, ptr="", names=False):
    """(pointer, keyword, reference, the schema at the pointer) for every reference in a schema."""
    if isinstance(value, dict):
        for keyword in ("$ref", "$dynamicRef"):
            if not names and isinstance(value.get(keyword), str):
                yield ptr, keyword, value[keyword], value
        for k, v in value.items():
            if names or k not in ("const", "enum", "default", "examples"):
                yield from references(v, pointer.join(ptr, k),
                                      not names and k in ("properties", "$defs", "patternProperties",
                                                          "dependentSchemas"))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from references(v, pointer.join(ptr, i))


def oracle(schema, uri):
    """ptr -> what `referencing` resolves each reference to, if it is installed."""
    referencing = pytest.importorskip("referencing")
    from urllib.parse import urljoin

    from referencing.exceptions import Unresolvable
    from referencing.jsonschema import DRAFT202012

    root = referencing.Resource.from_contents(schema, default_specification=DRAFT202012)
    base = urljoin(uri, root.id()) if root.id() else uri
    registry = referencing.Registry().with_resource(uri, root).with_resource(base, root).crawl()
    out = {}

    def walk(value, ptr, resolver, names=False):
        if isinstance(value, dict):
            if not names and ptr and isinstance(value.get("$id"), str):
                resolver = resolver.in_subresource(referencing.Resource.from_contents(value, DRAFT202012))
            for keyword in ("$ref", "$dynamicRef"):
                if not names and isinstance(value.get(keyword), str):
                    try:
                        out[(ptr, keyword)] = resolver.lookup(value[keyword]).contents
                    except Unresolvable:
                        out[(ptr, keyword)] = None
            for k, v in value.items():
                if names or k not in ("const", "enum", "default", "examples"):
                    walk(v, pointer.join(ptr, k), resolver,
                         not names and k in ("properties", "$defs", "patternProperties", "dependentSchemas"))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, pointer.join(ptr, i), resolver)

    walk(schema, "", registry.resolver(base_uri=base))
    return out


@pytest.mark.parametrize("schema", list(suite_cases()))
def test_suite_references_land_where_the_reference_implementation_does(schema, tmp_path):
    (tmp_path / "schema.json").write_text(json.dumps(schema))
    (tmp_path / "openapi.yaml").write_text(HEAD + "components:\n  schemas:\n    T: {$ref: 'schema.json'}\n")
    builder = _Builder([tmp_path / "openapi.yaml"], "x")
    builder.run()
    problems = [i for i in builder.issues if i.code != "unsupported-ref"]
    assert not problems, problems
    assert all(i.message.find("remote references are not supported") > 0 for i in builder.issues)
    expected = oracle(schema, (tmp_path / "schema.json").resolve().as_uri())
    for ptr, keyword, ref, _ in references(schema):
        mine = builder.resolved[("schema.json", ptr, keyword)]
        if mine is None:
            assert expected[(ptr, keyword)] is None or any(r in ref or r in str(schema.get("$id", ""))
                                                           for r in REMOTE), (ptr, ref)
        else:
            assert pointer.lookup(mine[0].data, mine[1]) == expected[(ptr, keyword)], (ptr, ref)


def test_id_sets_the_base_for_references_inside_it(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            Pet:
              $id: 'https://example.com/schemas/pet'
              properties:
                tag: {$ref: '#/$defs/Tag'}
                owner: {$ref: 'owner'}
              $defs:
                Tag: {properties: {label: {type: string}}}
            Owner:
              $id: 'https://example.com/schemas/owner'
              properties:
                name: {type: string}
            Order:
              properties:
                pet: {$ref: 'https://example.com/schemas/pet'}
        """)})
    assert codes(s) == []
    assert type_of(s, "v1:field/Pet.tag") == "openapi.yaml#/components/schemas/Pet/$defs/Tag"
    assert type_of(s, "v1:field/Pet.owner") == "openapi.yaml#/components/schemas/Owner"
    assert type_of(s, "v1:field/Order.pet") == "openapi.yaml#/components/schemas/Pet"


def test_anchor(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            Name: {$anchor: petName, type: string, minLength: 1}
            Pet:
              properties:
                name: {$ref: '#petName'}
        """)})
    assert codes(s) == []
    assert type_of(s, "v1:field/Pet.name") == "openapi.yaml#/components/schemas/Name"


def test_urn_ids(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            Pet:
              $id: 'urn:uuid:deadbeef-1234-ffff-ffff-4321feebdaed'
              properties:
                tag: {$ref: '#/$defs/Tag'}
              $defs:
                Tag: {type: string}
            Order:
              properties:
                pet: {$ref: 'urn:uuid:deadbeef-1234-ffff-ffff-4321feebdaed'}
        """)})
    assert codes(s) == []


def test_dynamic_ref_with_one_target_is_a_reference(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            Tree:
              $dynamicAnchor: node
              properties:
                value: {type: string}
                children: {type: array, items: {$dynamicRef: '#node'}}
        """)})
    assert codes(s) == []
    tree = s.graph.nodes["v1:schema/Tree"]
    items = [e.target for f in s.graph.outgoing(tree.id, g.HAS_FIELD)
             for t in s.graph.outgoing(f.target, g.FIELD_TYPE) for e in s.graph.outgoing(t.target, g.ITEMS)]
    assert items and any(e.target == tree.id and e.data["via"] == "$dynamicRef"
                         for i in items for e in s.graph.outgoing(i, g.INHERITS))


def test_dynamic_ref_with_several_targets_keeps_each_as_a_condition(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            List:
              $id: 'https://example.com/list'
              type: array
              items: {$dynamicRef: '#item'}
              $defs:
                item: {$dynamicAnchor: item}
            StringList:
              $id: 'https://example.com/strings'
              $ref: 'list'
              $defs:
                item: {$dynamicAnchor: item, type: string}
        """)})
    assert codes(s) == []
    items = next(e.target for e in s.graph.outgoing("v1:schema/List", g.ITEMS))
    whens = sorted(e.data["when"] for e in s.graph.outgoing(items, g.CONDITIONAL) if e.data["keyword"] == "$dynamicRef")
    assert whens == ["openapi.yaml#/components/schemas/List/$defs/item",
                     "openapi.yaml#/components/schemas/StringList/$defs/item"]


def test_duplicate_id(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            A: {$id: 'https://example.com/same', type: string}
            B: {$id: 'https://example.com/same', type: integer}
        """)})
    assert codes(s) == ["duplicate-id"]


def test_unknown_id_is_a_remote_reference(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            A: {$ref: 'https://example.com/nothing-has-this-id'}
        """)})
    assert codes(s) == ["unsupported-ref"] and "no loaded schema has $id" in s.issues[0].message


def test_openapi_30_has_no_ids(spec):
    s = spec({"openapi.yaml": "openapi: 3.0.3\ninfo: {title: t, version: '1'}\n" + dedent("""
        components:
          schemas:
            A: {$ref: 'https://example.com/a'}
            B: {type: string}
        """)})
    assert codes(s) == ["unsupported-ref"]


def test_a_relative_file_under_an_id_is_still_read(spec):
    # Tools read `$ref: 'pet.yaml'` as the file next to this one even under an `$id`, if the file is there.
    s = spec({"openapi.yaml": HEAD + dedent("""
                components:
                  schemas:
                    Order:
                      $id: 'https://example.com/schemas/order'
                      properties:
                        pet: {$ref: 'pet.yaml'}
                """), "pet.yaml": "type: object\nproperties: {name: {type: string}}\n"})
    assert codes(s) == []
    assert type_of(s, "v1:field/Order.pet") == "pet.yaml#"


def test_a_field_that_is_only_a_dynamic_ref(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            Node:
              $dynamicAnchor: node
              properties:
                next: {$dynamicRef: '#node'}
        """)})
    assert codes(s) == []
    [t] = s.graph.outgoing("v1:field/Node.next", g.FIELD_TYPE)
    assert [e.target for e in s.graph.outgoing(t.target, g.INHERITS)] == ["v1:schema/Node"]


def test_a_reference_to_an_id_does_not_depend_on_which_file_is_read_first(spec):
    x, y = "x: {$ref: 'https://example.com/late'}\n", "y: {$ref: 'late.yaml'}\n"
    late = "$id: 'https://example.com/late'\nproperties: {name: {type: string}}\n"
    for other in (x + y, y + x):
        s = spec({"openapi.yaml": HEAD + dedent("""
                components:
                  schemas:
                    A: {properties: {x: {$ref: 'other.yaml#/x'}, y: {$ref: 'other.yaml#/y'}}}
                """), "other.yaml": other, "late.yaml": late})
        assert codes(s) == [], other
        assert type_of(s, "v1:field/A.x") == type_of(s, "v1:field/A.y") == "late.yaml#"


def test_the_same_id_on_two_files(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
                components:
                  schemas:
                    A: {$ref: 'a.yaml'}
                    B: {$ref: 'b.yaml'}
                """), "a.yaml": "$id: 'https://example.com/s'\ntype: string\n",
              "b.yaml": "$id: 'https://example.com/s'\ntype: integer\n"})
    assert codes(s) == ["duplicate-id"]


@pytest.mark.parametrize("name", ["pet(v2).yaml", "my pet.yaml", "pet+v2@x.yaml"])
def test_an_anchor_in_a_file_whose_name_is_escaped_in_a_uri(spec, name):
    s = spec({"openapi.yaml": HEAD + dedent(f"""
                components:
                  schemas:
                    A: {{properties: {{pet: {{$ref: '{name}#pet'}}}}}}
                """), name: "$defs:\n  P: {$anchor: pet, type: string}\n"})
    assert codes(s) == []
    assert type_of(s, "v1:field/A.pet") == f"{name}#/$defs/P"


def test_a_discriminator_name_under_an_id_is_a_component(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            Pet:
              $id: 'https://example.com/pet'
              oneOf: [{$ref: 'openapi.yaml#/components/schemas/Dog'}]
              discriminator: {propertyName: kind, mapping: {dog: Dog}}
            Dog: {properties: {kind: {type: string}}}
        """)})
    assert codes(s) == []
    [option] = s.graph.outgoing("v1:schema/Pet", g.OPTION)
    assert option.target == "v1:schema/Dog" and option.data["values"] == ["dog"]


def test_a_missing_file_under_an_id_says_so(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            T: {$id: 'https://example.com/t', properties: {a: {$ref: 'missing.yaml'}}}
        """)})
    assert codes(s) == ["unsupported-ref"] and "no file 'missing.yaml' next to openapi.yaml" in s.issues[0].message


def test_openapi_30_reports_and_ignores_2020_12_keywords(spec):
    s = spec({"openapi.yaml": "openapi: 3.0.3\ninfo: {title: t, version: '1'}\n" + dedent("""
        components:
          schemas:
            A:
              $id: 'https://example.com/a'
              properties:
                next: {$dynamicRef: '#node'}
        """)})
    assert codes(s) == ["unsupported-keyword", "unsupported-keyword"]
    assert not s.graph.outgoing("v1:field/A.next", g.FIELD_TYPE)


def test_a_dynamic_anchor_nothing_leads_from_is_not_a_target(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            List:
              $id: 'https://example.com/list'
              type: array
              items: {$dynamicRef: '#item'}
              $defs:
                item: {$dynamicAnchor: item}
            Unrelated:
              $id: 'https://example.com/unrelated'
              $defs:
                item: {$dynamicAnchor: item, type: integer}
        """)})
    assert codes(s) == []
    items = next(e.target for e in s.graph.outgoing("v1:schema/List", g.ITEMS))
    assert not s.graph.outgoing(items, g.CONDITIONAL)
    assert [e.data["via"] for e in s.graph.outgoing(items, g.INHERITS)] == ["$dynamicRef"]
