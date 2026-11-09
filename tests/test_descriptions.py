"""Descriptions that state a limit the schema contradicts."""

from textwrap import dedent

from speclens.cli import main
from speclens.descriptions import description_issues

HEAD = "openapi: 3.1.0\ninfo: {title: t, version: '1'}\n"

SPEC = HEAD + dedent("""
    paths:
      /pets:
        get:
          parameters:
            - {name: limit, in: query, description: At most 50 per page., schema: {type: integer, maximum: 100}}
            - {name: page, in: query, description: Required., schema: {type: integer, minimum: 1}}
          responses: {'200': {description: ok}}
    components:
      schemas:
        Pet:
          description: A pet. The name must not exceed 40 characters.
          properties:
            name: {type: string, maxLength: 60}
            age: {type: integer, minimum: 0, maximum: 30, description: Between 0 and 30 years.}
            rank: {type: integer, minimum: 1, maximum: 10, description: A number from 1 to 5.}
            size: {type: integer, description: From 1 to 3.}
            tag: {type: string, maxLength: 10, description: Up to 10 characters; required for show pets.}
        Dog:
          allOf:
            - $ref: '#/components/schemas/Pet'
    """)


def test_a_description_whose_limit_the_schema_contradicts_is_a_warning(spec):
    s = spec({"openapi.yaml": SPEC})
    found = {i.message.split(":", 1)[0]: i for i in description_issues(s)}
    # The schema's own description, naming its field in plain words; a field's; a parameter's.
    assert "the schema says name: maxLength 60" in found["Pet"].message
    assert found["Pet"].severity == "warning" and found["Pet"].file == "openapi.yaml"
    assert "the schema says rank: maximum 10" in found["Pet.rank"].message
    assert "GET /pets query limit" in found
    # Agreeing limits, a limit the schema does not state (it may be enforced elsewhere), an inherited field
    # (said once, where written), and "Required" in prose (most often a condition) are not warnings.
    assert not {"Pet.age", "Pet.size", "Pet.tag", "Dog.name", "Dog.rank", "GET /pets query page"} & set(found)


def test_validate_shows_them_and_still_passes(write, capsys):
    root = write({"openapi.yaml": SPEC})
    assert main(["validate", str(root / "openapi.yaml")]) == 0  # warnings do not fail validation
    out = capsys.readouterr().out
    assert "warning [description-contradicts-schema] openapi.yaml:" in out
    assert "the description says 'A number from 1 to 5.'" in out
