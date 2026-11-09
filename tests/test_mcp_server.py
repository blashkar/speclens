"""The MCP server, through the MCP client in the same process: every tool, errors, evidence and load_spec."""

import asyncio
import sys
from pathlib import Path
from textwrap import dedent

import pytest

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402

from speclens import SpecSet  # noqa: E402
from speclens.mcp_server import _parse, build_server  # noqa: E402

STARTER = Path(__file__).parent / "fixtures" / "openapi-starter" / "openapi.yaml"

V1 = """\
    openapi: 3.1.0
    info: {title: pets, version: '1.2'}
    paths:
      /pets/{petId}:
        get:
          operationId: getPet
          parameters:
            - {name: petId, in: path, required: true, schema: {type: string}}
          responses:
            '200':
              content:
                application/json:
                  schema: {$ref: '#/components/schemas/Pet'}
    components:
      schemas:
        Pet:
          required: [name]
          properties:
            name: {type: string, description: The pet's name.}
            birthDate: {type: string, format: date}
            tag: {type: string}
    """


def call(server, *calls):
    """Run tool calls in one session; returns (text, is_error) for each."""
    async def run():
        out = []
        async with Client(server) as client:
            for name, arguments in calls:
                r = await client.call_tool(name, arguments)
                out.append(("\n".join(c.text for c in r.content), r.is_error))
        return out
    return asyncio.run(run())


@pytest.fixture
def pets(write):
    root = write({"v1/openapi.yaml": V1, "v2/openapi.yaml": V1.replace("'1.2'", "'1.10'")
                  .replace("required: [name]", "required: [tag]")
                  .replace("            name: {type: string, description: The pet's name.}\n", "")})
    specs = SpecSet()
    specs.load(root / "v1" / "openapi.yaml")
    specs.load(root / "v2" / "openapi.yaml")
    return specs


def test_tools_are_listed_read_only(pets):
    async def run():
        async with Client(build_server(pets)) as client:
            return (await client.list_tools()).tools
    tools = {t.name: t for t in asyncio.run(run())}
    assert set(tools) == {"list_specs", "names_in", "get_schema", "get_operation", "find_field", "list_operations",
                          "search", "source", "compare", "diff", "validate", "verify", "clear_evidence"}
    assert all(t.annotations.read_only_hint for t in tools.values())
    assert "call it first" in tools["names_in"].description


def test_queries(pets):
    out = call(build_server(pets),
               ("list_specs", {}),
               ("names_in", {"text": "when was the birth date added to a pet?", "label": "1.2"}),
               ("get_schema", {"name": "Pet", "label": "1.2"}),
               ("get_operation", {"operation": "GET /pets/{petId}", "label": "1.10"}),
               ("find_field", {"name": "tag", "label": "1.2"}),
               ("list_operations", {"label": "1.2"}),
               ("search", {"text": "pet name", "label": "1.2", "limit": 3}),
               ("source", {"id": "1.2:field/Pet.name"}))
    assert all(not error for _, error in out)
    specs, names, schema, operation, field, operations, search, source = (t for t, _ in out)
    assert specs.splitlines() == ["1.2: OpenAPI 3.1, 1 operations, 1 schemas, 3 fields; 0 errors, 0 warnings",
                                  "1.10: OpenAPI 3.1, 1 operations, 1 schemas, 2 fields; 0 errors, 0 warnings"]
    assert "'birth date' -> field 1.2:field/Pet.birthDate" in names
    assert schema.startswith("schema Pet [1.2:schema/Pet") and "The pet's name." in schema
    assert operation.startswith("operation GET /pets/{petId} [1.10:operation/GET /pets/{petId}")
    assert "1.2:field/Pet.tag" in field
    assert "GET /pets/{petId}" in operations
    assert "Pet" in search
    assert "name: {type: string, description: The pet's name.}" in source


def test_errors_are_tool_errors_with_the_closest_names(pets):
    (text, error), (text2, error2) = call(build_server(pets), ("get_schema", {"name": "Pett", "label": "1.2"}),
                                          ("get_schema", {"name": "Pet"}))
    assert error and "closest: Pet" in text
    assert error2 and "say which spec; loaded: 1.2, 1.10" in text2


def test_compare_and_diff(pets):
    out = call(build_server(pets), ("compare", {"name": "Pet", "old_label": "1.2", "new_label": "1.10"}),
               ("diff", {"old_label": "1.2", "new_label": "1.10"}),
               ("diff", {"old_label": "1.2", "new_label": "1.10", "breaking_only": True}))
    compare, diff, breaking = (t for t, _ in out)
    assert "breaking: required field removed: name" in compare
    assert "breaking: 1 change in 1 operation" in diff or "breaking: 1 changes in 1 operations" in diff
    assert breaking.startswith("1 breaking changes from 1.2 to 1.10:") and "GET /pets/{petId}" in breaking


def test_verify_checks_against_the_results_returned(pets):
    server = build_server(pets)
    out = call(server, ("verify", {"answer": "Pet has `name`."}),
               ("get_schema", {"name": "Pet", "label": "1.2"}),
               ("verify", {"answer": "Pet has `name` [1.2:field/Pet.name] and `color` [1.2:field/Pet.color]."}),
               ("verify", {"answer": "The Pet schema has 3 fields."}),
               ("verify", {"answer": "The Pet schema has 5 fields."}),
               ("clear_evidence", {}),
               ("verify", {"answer": "Pet has `name`.", "evidence_text": ["Pet: name, tag"]}))
    (none, none_error), _, (cited, _), (right, _), (wrong, _), (cleared, _), (text, _) = out
    assert none_error and "no evidence" in none
    assert "unknown-citation: '1.2:field/Pet.color'" in cited and "unknown-name: 'color'" in cited
    assert "1.2:field/Pet.name" not in cited
    assert right.endswith("1 counts: no findings")
    assert "wrong-count" in wrong
    assert cleared == "forgot 1 results"
    assert "unknown-name" not in text


def test_validate(write):
    root = write({"openapi.yaml": V1.replace("#/components/schemas/Pet", "#/components/schemas/Nope")})
    specs = SpecSet()
    specs.load(root / "openapi.yaml", "v1")
    (text, error), = call(build_server(specs), ("validate", {}))
    assert not error and text.startswith("v1: 1 errors") and "Nope" in text


def test_load_spec_only_from_allowed_folders(write):
    root = write({"api/openapi.yaml": V1, "other/openapi.yaml": V1, "secret.yaml": "Pet: {type: object}\n",
                  "api/leak/openapi.yaml": V1.replace("$ref: '#/components/schemas/Pet'", "$ref: '../../secret.yaml#/Pet'")})
    specs = SpecSet()
    server = build_server(specs, [root / "api"])
    out = call(server, ("load_spec", {"path": str(root / "api"), "label": "a"}),
               ("load_spec", {"path": str(root / "other" / "openapi.yaml")}),
               ("load_spec", {"path": str(root / "api" / ".." / "other")}),
               ("load_spec", {"path": str(root / "api" / "missing.yaml")}),
               ("get_schema", {"name": "Pet", "label": "a"}),
               ("load_spec", {"path": str(root / "api" / "leak"), "label": "leak"}),
               ("list_specs", {}))
    assert out[0] == ("loaded a: OpenAPI 3.1, 0 errors", False)
    assert out[1][1] and "not under a folder" in out[1][0]
    assert out[2][1] and "not under a folder" in out[2][0]  # no way out with ..
    assert out[3][1] and "does not exist" in out[3][0]
    assert out[4][0].startswith("schema Pet [a:schema/Pet")
    assert out[5][1] and "references files outside" in out[5][0]  # a $ref cannot reach out either
    assert "leak" not in out[6][0]
    # ... and the file outside is never read.
    from speclens import load_spec
    leak = load_spec(root / "api" / "leak", allow=[root / "api"])
    [outside] = [d for d in leak.documents.values() if d.name.endswith("secret.yaml")]
    assert outside.text == "" and [i.code for i in outside.issues] == ["outside-allowed"]


def test_without_allow_there_is_no_load_spec(pets):
    (text, error), = call(build_server(pets), ("load_spec", {"path": "/"}))
    assert error and "load_spec" in text


def test_public_spec():
    specs = SpecSet()
    specs.load(STARTER, "1")
    out = call(build_server(specs), ("list_specs", {}), ("list_operations", {}))
    assert all(not e for _, e in out) and "0 errors" in out[0][0] and out[1][0].strip()


def test_spec_arguments():
    assert _parse("api/openapi.yaml") == ("api/openapi.yaml", None)
    assert _parse("api/=2.0") == ("api/", "2.0")
    assert _parse("a.yaml,b.yaml=v1") == (["a.yaml", "b.yaml"], "v1")
    assert _parse("/data/team=api/openapi.yaml") == ("/data/team=api/openapi.yaml", None)  # "=" in a path


def test_verify_evidence_is_what_was_shown(pets):
    out = call(build_server(pets), ("diff", {"old_label": "1.2", "new_label": "1.10", "breaking_only": True}),
               ("verify", {"answer": "In 1.10 `tag` became required."}))
    assert "unknown-name: 'tag'" in out[1][0]  # only the breaking change (name removed) was shown


def test_validate_results_are_evidence(write):
    root = write({"openapi.yaml": V1.replace("#/components/schemas/Pet", "#/components/schemas/Nope")})
    specs = SpecSet()
    specs.load(root / "openapi.yaml", "v1")
    _, (text, _) = call(build_server(specs), ("validate", {}), ("verify", {"answer": "`Nope` is not defined."}))
    assert text.endswith("no findings"), text


def test_verify_with_known_and_cover_changes(pets):
    out = call(build_server(pets), ("compare", {"name": "Pet", "old_label": "1.2", "new_label": "1.10"}),
               ("verify", {"answer": "`Vet` is unchanged.", "known": ["`Vet` was in the last answer."]}),
               ("verify", {"answer": "Pet changed.", "cover_changes": True}),
               ("verify", {"answer": "`name` was removed from Pet.", "cover_changes": True}))
    assert "unknown-name" not in out[1][0]
    assert "omitted-change" in out[2][0] and "name" in out[2][0]
    assert "omitted-change" not in out[3][0]


def test_get_operation_with_expand_and_the_schemas_are_evidence(pets):
    [(text, error), (verdict, _)] = call(build_server(pets),
        ("get_operation", {"operation": "GET /pets/{petId}", "label": "1.2", "expand": 1}),
        ("verify", {"answer": "The Pet schema has 3 fields."}))
    assert not error and "referenced schemas (1):" in text and "\nschema Pet [1.2:schema/Pet" in text
    assert "1 counts: no findings" in verdict
