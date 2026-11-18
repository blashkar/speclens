"""Comparing two specs: each kind of change, how it is rated on each side, and the whole-spec diff."""

from textwrap import dedent

import pytest

from speclens import NotFound, SpecSet
from speclens.cli import main

HEAD = "openapi: 3.1.0\ninfo: {title: t, version: '1'}\n"

OLD = HEAD + dedent("""
    paths:
      /pets:
        get:
          parameters:
            - {name: limit, in: query, schema: {type: integer, maximum: 100}}
            - {name: tag, in: query, schema: {type: string}}
          responses:
            '200':
              content:
                application/json:
                  schema: {type: array, items: {$ref: '#/components/schemas/Pet'}}
        post:
          requestBody:
            content:
              application/json:
                schema: {$ref: '#/components/schemas/NewPet'}
          responses:
            '201': {description: created}
      /pets/{id}:
        delete:
          parameters:
            - {name: id, in: path, required: true, schema: {type: string}}
          responses:
            '204': {description: gone}
      /stores:
        get:
          responses:
            '200': {description: ok}
    components:
      schemas:
        Pet:
          required: [id, name]
          properties:
            id: {type: integer}
            name: {type: string}
            status: {type: string, enum: [available, sold]}
            nickname: {type: string}
            owner: {$ref: '#/components/schemas/Owner'}
            friend: {$ref: '#/components/schemas/Pet'}
        Owner:
          properties:
            email: {type: string, format: email}
            phone: {type: string}
        NewPet:
          properties:
            name: {type: string, maxLength: 50}
            kind: {type: string, enum: [cat, dog, bird]}
    """)

NEW = HEAD + dedent("""
    paths:
      /pets:
        get:
          parameters:
            - {name: limit, in: query, schema: {type: integer, maximum: 50}}
            - {name: region, in: query, required: true, schema: {type: string}}
          responses:
            '200':
              content:
                application/json:
                  schema: {type: array, items: {$ref: '#/components/schemas/Pet'}}
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema: {$ref: '#/components/schemas/NewPet'}
          responses:
            '201': {description: created}
      /pets/{id}:
        delete:
          deprecated: true
          parameters:
            - {name: id, in: path, required: true, schema: {type: string}}
          responses:
            '204': {description: gone}
      /owners:
        get:
          responses:
            '200': {description: ok}
    components:
      schemas:
        Pet:
          required: [id]
          properties:
            id: {type: number}
            name: {type: string}
            status: {type: string, enum: [available, sold, pending]}
            owner: {$ref: '#/components/schemas/Owner'}
            friend: {$ref: '#/components/schemas/Pet'}
            age: {type: integer}
        Owner:
          properties:
            email: {type: string, format: idn-email}
        NewPet:
          required: [kind]
          properties:
            name: {type: string, maxLength: 20}
            kind: {type: string, enum: [cat, dog]}
    """)


@pytest.fixture
def specs(write):
    root = write({"old/openapi.yaml": OLD, "new/openapi.yaml": NEW})
    s = SpecSet()
    s.load(root / "old/openapi.yaml", "1.2")
    s.load(root / "new/openapi.yaml", "1.10")
    return s


def kinds(comparison):
    return {(c.kind, c.where, c.level) for c in comparison.changes}


def test_a_response_schema(specs):
    r = specs.compare("Pet", "1.2", "1.10")
    assert r.side == "response"
    assert kinds(r) == {
        ("field-removed", "nickname", "info"),  # optional: readers already cope with it missing
        ("field-added", "age", "info"),
        ("type-widened", "id", "breaking"),  # integer -> number: a reader expecting integers can fail
        ("required-removed", "name", "breaking"),
        ("enum-value-added", "status", "breaking"),  # a reader handling every known value can fail
        ("field-removed", "owner.phone", "info"),  # nested, through Owner
        ("format-changed", "owner.email", "breaking"),
    }
    text = r.to_text()
    assert text.splitlines()[0] == "compare schema Pet: 1.2 -> 1.10 [1.2:schema/Pet -> 1.10:schema/Pet]"
    assert "used in: responses" in text
    assert "7 changes (4 breaking, 0 warning, 3 info):" in text
    assert "  info: field removed: nickname (string -> none) [1.2:field/Pet.nickname]" in text


def test_a_request_schema_is_rated_for_requests(specs):
    r = specs.compare("NewPet", "1.2", "1.10")
    assert r.side == "request"
    assert kinds(r) == {
        ("required-added", "kind", "breaking"),
        ("enum-value-removed", "kind", "breaking"),
        ("constraint-tightened", "name", "breaking"),
    }
    [c] = [c for c in r.changes if c.kind == "constraint-tightened"]
    assert (c.before, c.after) == ("maxLength 50", "maxLength 20")


def test_an_operation(specs):
    r = specs.compare("GET /pets", "1.2", "1.10")
    found = kinds(r)
    assert ("required-parameter-added", "parameter query region", "breaking") in found
    assert ("parameter-removed", "parameter query tag", "warning") in found
    assert ("constraint-tightened", "parameter query limit", "breaking") in found
    # Changes inside the response, rated for responses, with the way to them.
    assert ("required-removed", "response 200 application/json[]: name", "breaking") in found
    assert ("format-changed", "response 200 application/json[]: owner.email", "breaking") in found
    post = specs.compare("post /pets", "1.2", "1.10")
    assert ("required-added", "request body", "breaking") in kinds(post)
    assert ("required-added", "request application/json: kind", "breaking") in kinds(post)
    assert kinds(specs.compare("DELETE /pets/{id}", "1.2", "1.10")) == {("deprecated", "", "info")}


def test_a_cycle_is_walked_once(specs):
    # Pet.friend is a Pet: its changes are reported once, under Pet itself, not again under friend.
    r = specs.compare("Pet", "1.2", "1.10")
    assert not any(c.where.startswith("friend") for c in r.changes)


def test_added_and_removed(specs):
    assert kinds(specs.compare("GET /owners", "1.2", "1.10")) == {("operation-added", "", "info")}
    assert kinds(specs.compare("GET /stores", "1.2", "1.10")) == {("operation-removed", "", "breaking")}
    r = specs.compare("Pet.nickname", "1.2", "1.10")
    assert (r.what, kinds(r)) == ("field", {("field-removed", "", "info")})  # optional, in responses
    assert specs.compare("Pet.name", "1.2", "1.10").changes == []
    with pytest.raises(NotFound):
        specs.compare("Nothing", "1.2", "1.10")


def test_no_changes(specs):
    r = specs.compare("Pet", "1.2", "1.2")
    assert r.changes == []
    assert r.to_text().endswith("no changes")


def test_whole_spec_diff(specs):
    d = specs.diff("1.2", "1.10")
    assert (d.operations_added, d.operations_removed) == (["GET /owners"], ["GET /stores"])
    assert sorted(d.operations_changed) == ["DELETE /pets/{id}", "GET /pets", "POST /pets"]
    assert sorted(d.schemas_changed) == ["NewPet", "Owner", "Pet"]
    broken = {op for op, _ in d.breaking()}
    assert broken == {"GET /pets", "POST /pets", "GET /stores"}
    text = d.to_text()
    assert "operations: 1 added, 1 removed, 3 changed" in text
    assert "breaking:" in text and "  breaking: operation removed: GET /stores" in text


def test_a_type_written_inline_or_as_a_schema(write):
    old = HEAD + dedent("""
        components:
          schemas:
            Issue:
              properties:
                role: {type: string}
                author: {type: object, properties: {login: {type: string}, id: {type: integer}}}
        """)
    new = HEAD + dedent("""
        components:
          schemas:
            Issue:
              properties:
                role: {$ref: '#/components/schemas/Role'}
                author: {$ref: '#/components/schemas/User'}
            Role: {type: string, enum: [owner, member]}
            User: {type: object, properties: {login: {type: string}}}
        """)
    root = write({"a/openapi.yaml": old, "b/openapi.yaml": new})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    # A string that became a named enum of strings is a tightening, not a new type...
    # ...and an inline object that became a named one is compared by what it holds.
    assert kinds(s.compare("Issue", "a", "b")) == {
        ("constraint-tightened", "role", "breaking"),  # not used anywhere: rated for the worse side
        ("field-removed", "author.id", "warning"),  # optional; unused, so the worse of request and response
    }


def test_cli(write, capsys):
    root = write({"old/openapi.yaml": OLD, "new/openapi.yaml": NEW})
    old, new = str(root / "old/openapi.yaml"), str(root / "new/openapi.yaml")
    assert main(["compare", old, new, "Pet"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("compare schema Pet: old -> new")  # both say version 1, so old and new
    assert main(["diff", old, new]) == 0
    assert main(["diff", old, new, "--breaking"]) == 1  # for CI: fail when something breaks
    capsys.readouterr()
    assert main(["diff", old, old, "--breaking"]) == 0
    assert main(["compare", old, new, "GET /pets", "--json"]) == 0
    assert '"kind": "required-parameter-added"' in capsys.readouterr().out


def test_wrapped_in_one_of_is_breaking_only_if_no_option_takes_the_old_shape(write):
    def body(schema):
        return HEAD + dedent(f"""
            paths:
              /teams:
                post:
                  requestBody:
                    content:
                      application/json:
                        schema: {schema}
                  responses:
                    '200': {{description: ok}}
            """)
    array = "{type: array, items: {type: string}}"
    obj = "{type: object, properties: {names: {type: array, items: {type: string}}}}"
    tight = "{type: object, properties: {names: {type: array, minItems: 1, items: {type: string}}}}"
    root = write({"a/openapi.yaml": body(array), "b/openapi.yaml": body(f"{{oneOf: [{obj}, {array}]}}"),
                  "c/openapi.yaml": body(obj), "d/openapi.yaml": body(f"{{oneOf: [{tight}, {array}]}}")})
    s = SpecSet()
    for label in "abcd":
        s.load(root / f"{label}/openapi.yaml", label)
    # An array is still one of the options: every old payload is still valid.
    assert kinds(s.compare("POST /teams", "a", "b")) == {
        ("options-introduced-compatible", "request application/json", "info")}
    # The matching option now needs at least one name: an empty list that was valid is not.
    assert kinds(s.compare("POST /teams", "c", "d")) == {
        ("options-introduced", "request application/json", "breaking")}


def test_nullable_beside_all_of_is_still_nullable_as_a_ref(write):
    # Written two ways, the same meaning: nothing changed. (A checker that merges allOf and drops the
    # `nullable` beside it reports "became nullable" here.)
    old = HEAD + dedent("""
        components:
          schemas:
            Repo:
              properties:
                license:
                  nullable: true
                  allOf: [{$ref: '#/components/schemas/License'}]
            License: {type: object, properties: {key: {type: string}}}
        """).replace("openapi: 3.1.0", "openapi: 3.0.3")
    new = HEAD + dedent("""
        components:
          schemas:
            Repo:
              properties:
                license: {$ref: '#/components/schemas/NullableLicense'}
            NullableLicense: {type: object, nullable: true, properties: {key: {type: string}}}
        """).replace("openapi: 3.1.0", "openapi: 3.0.3")
    root = write({"a/openapi.yaml": old, "b/openapi.yaml": new})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    assert not [c for c in s.compare("Repo", "a", "b").changes if c.level != "info"]


def test_a_field_moved_to_a_parent_says_so(write):
    # The same field, now written on the parent the schema inherits from: not a field gone and another come,
    # and not only a new description.
    def spec(on_parent):
        return HEAD + dedent(f"""
            components:
              schemas:
                Station:
                  properties:
                    id: {{type: string}}
                    {"keeper: {type: string, description: Who keeps the station.}" if on_parent else ""}
                Lighthouse:
                  allOf:
                    - $ref: '#/components/schemas/Station'
                    - properties:
                        height: {{type: number}}
                        {"" if on_parent else "keeper: {type: string, description: Who keeps it.}"}
            """)
    root = write({"a/openapi.yaml": spec(False), "b/openapi.yaml": spec(True)})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    moved = [c for c in s.compare("Lighthouse", "a", "b").changes if c.kind == "field-moved"]
    assert [(c.where, c.before, c.after, c.level) for c in moved] == [
        ("keeper", "own", "inherited from Station", "info")]
    assert "field moved: keeper (own -> inherited from Station [b:schema/Station])" in s.compare("Lighthouse", "a", "b").to_text()
    assert ("field-moved", "", "info") in kinds(s.compare("Lighthouse.keeper", "a", "b"))
    assert "field moved: keeper (inherited from Station [b:schema/Station] -> own)" in s.compare("Lighthouse", "b", "a").to_text()
    assert not [c for c in s.compare("Lighthouse", "a", "b").changes if c.kind.startswith("field-")
                and c.kind != "field-moved"]


def test_fields_moved_together_are_one_line(write):
    def spec(on_parent):
        moved = "\n".join(f"{n}: {{type: string}}" for n in ("born", "code", "country"))
        parent, own = (moved, "") if on_parent else ("", moved)
        return HEAD + "components:\n  schemas:\n" + "".join(
            f"    {name}:\n" + body for name, body in (
                ("Person", "      properties:\n        id: {type: string}\n"
                 + "".join(f"        {line}\n" for line in parent.splitlines())),
                ("Customer", "      allOf:\n        - $ref: '#/components/schemas/Person'\n"
                 "        - properties:\n            note: {type: string}\n"
                 + "".join(f"            {line}\n" for line in own.splitlines()))))
    root = write({"a/openapi.yaml": spec(False), "b/openapi.yaml": spec(True)})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    r = s.compare("Customer", "a", "b")
    assert [c.where for c in r.changes if c.kind == "field-moved"] == ["born", "code", "country"]  # each in JSON
    text = r.to_text()
    assert "3 changes (0 breaking, 0 warning, 3 info)" in text
    # one line, citing the parent they all moved to
    assert "field moved: born, code, country (own -> inherited from Person [b:schema/Person])\n" in text + "\n"
    assert text.count("field moved") == 1


def test_a_removed_schema_that_looks_renamed_gets_a_hint(write):
    fields = "{id: {type: string}, distance: {type: integer}, speed: {type: number}, eta: {type: string}}"
    old = HEAD + dedent(f"""
        components:
          schemas:
            TripLeg: {{properties: {fields}}}
            Color: {{type: string, enum: [red, green, blue, black]}}
            Gone: {{properties: {{a: {{type: string}}, b: {{type: string}}, c: {{type: string}}}}}}
        """)
    new = HEAD + dedent(f"""
        components:
          schemas:
            RouteLeg: {{properties: {fields}}}
            Colour: {{type: string, enum: [red, green, blue, black, white]}}
            Other: {{properties: {{x: {{type: string}}, y: {{type: string}}, z: {{type: string}}}}}}
        """)
    root = write({"a/openapi.yaml": old, "b/openapi.yaml": new})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    d = s.diff("a", "b")
    assert d.possible_renames == {"TripLeg": {"to": "RouteLeg", "why": "4 of 4 fields kept"},
                                  "Color": {"to": "Colour", "why": "4 of 4 enum values kept, 1 new"}}
    assert "possibly renamed: TripLeg -> RouteLeg (4 of 4 fields kept)" in d.to_text()
    # From either side, as a hint beside the plain fact.
    [removed, hint] = s.compare("TripLeg", "a", "b").changes
    assert (removed.kind, hint.kind, hint.before, hint.after, hint.level) == \
        ("schema-removed", "possibly-renamed", "TripLeg", "RouteLeg", "info")
    assert [c.kind for c in s.compare("RouteLeg", "a", "b").changes] == ["schema-added", "possibly-renamed"]
    assert [c.kind for c in s.compare("Gone", "a", "b").changes] == ["schema-removed"]  # nothing alike


def test_the_same_change_in_many_places_is_shown_once(write):
    old = HEAD + dedent("""
        paths:
          /forms:
            post:
              requestBody:
                content:
                  application/json:
                    schema:
                      properties:
                        a: {$ref: '#/components/schemas/FormA'}
                        b: {$ref: '#/components/schemas/FormB'}
                        c: {$ref: '#/components/schemas/FormC'}
              responses:
                '200': {description: ok}
        components:
          schemas:
            Form: {properties: {year: {type: integer}}}
            FormA: {allOf: [{$ref: '#/components/schemas/Form'}]}
            FormB: {allOf: [{$ref: '#/components/schemas/Form'}]}
            FormC: {allOf: [{$ref: '#/components/schemas/Form'}]}
        """)
    new = old.replace("year: {type: integer}", "year: {type: integer, minimum: 2018}")
    root = write({"a/openapi.yaml": old, "b/openapi.yaml": new})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    r = s.compare("POST /forms", "a", "b")
    assert len(r.changes) == 3  # every way to it, in the data
    text = r.to_text()
    assert "3 changes, 1 distinct (3 breaking, 0 warning, 0 info):" in text
    assert "request application/json: a.year (minimum none -> minimum 2018) [b:field/Form.year] " \
           "(and 2 more places)" in text
    assert "breaking: 3 changes (1 distinct) in 1 operation:" in s.diff("a", "b").to_text()


def test_a_typed_response_that_becomes_anything(write):
    def spec(error, body):
        return HEAD + dedent(f"""
            paths:
              /jobs:
                post:
                  {body}
                  responses:
                    '200': {{description: ok}}
                    '500':
                      content:
                        application/json:
                          schema: {{$ref: '#/components/schemas/Failure'}}
            components:
              schemas:
                Failure: {error}
            """)
    body = "requestBody: {content: {application/json: {schema: {type: object}}}}"
    root = write({"a/openapi.yaml": spec("{type: object, properties: {message: {type: string}}}", body),
                  "b/openapi.yaml": spec("{}", "")})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    found = kinds(s.compare("POST /jobs", "a", "b"))
    assert ("became-any", "response 500 application/json", "breaking") in found
    assert ("request-body-removed", "request body", "breaking") in found


def test_constraints_that_are_not_types_are_not_anything(write):
    def spec(schema):
        return HEAD + dedent(f"""
            paths:
              /jobs:
                get:
                  responses:
                    '200':
                      content:
                        application/json:
                          schema: {schema}
            """)
    cases = [("{type: integer}", "{multipleOf: 5}"), ("{multipleOf: 5}", "{}"),
             ("{maxProperties: 3}", "{}"), ("{additionalProperties: false}", "{}")]
    found = []
    for a, b in cases:
        root = write({"a/openapi.yaml": spec(a), "b/openapi.yaml": spec(b)})
        s = SpecSet()
        s.load(root / "a/openapi.yaml", "a")
        s.load(root / "b/openapi.yaml", "b")
        found.append([k for k, _, _ in kinds(s.compare("GET /jobs", "a", "b"))])
    assert "became-any" not in found[0]
    assert all("became-any" in f for f in found[1:]), found


def test_options_unwrapped_from_one_property_objects(write):
    def spec(version, options, extra=""):
        return HEAD.replace("version: '1'", f"version: '{version}'") + dedent(f"""
            paths:
              /items:
                get:
                  responses:
                    '200':
                      content:
                        application/json:
                          schema: {{$ref: '#/components/schemas/Item'}}
            components:
              schemas:
                Item:
                  oneOf: [{options}]
                Circle: {{properties: {{radius: {{type: number}}}}}}
                Square: {{properties: {{side: {{type: number}}{extra}}}}}
                Triangle: {{properties: {{base: {{type: number}}}}}}
                Note: {{type: object, properties: {{text: {{type: string}}}}}}
            """)
    wrapped = ("{type: object, properties: {circle: {$ref: '#/components/schemas/Circle'}}}, "
               "{type: object, properties: {square: {$ref: '#/components/schemas/Square'}}}, "
               "{type: object, properties: {note: {type: string}}}")
    plain = ("{$ref: '#/components/schemas/Circle'}, {$ref: '#/components/schemas/Square'}, "
             "{$ref: '#/components/schemas/Triangle'}")
    root = write({"a/openapi.yaml": spec("1.0", wrapped),
                  "b/openapi.yaml": spec("2.0", plain, ", corner: {type: string}")})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "1.0")
    s.load(root / "b/openapi.yaml", "2.0")
    c = s.compare("GET /items", "1.0", "2.0")
    lines = c.to_text()
    assert "breaking: options unwrapped: response 200 application/json ({circle: Circle} -> Circle, " \
           "{square: Square} -> Square)" in lines, lines
    found = kinds(c)
    assert ("option-added", "response 200 application/json", "breaking") in found  # Triangle is new
    assert [k for k, _, _ in found].count("option-removed") == 1  # {note: string} wraps no schema
    assert "option-added" not in [k for k, w, _ in found if "Circle" in w or "Square" in w]
    assert any(k == "field-added" and "corner" in w for k, w, _ in found)  # Square itself is still compared
    # The reverse is wrapping.
    back = s.compare("GET /items", "2.0", "1.0")
    assert "options wrapped: response 200 application/json (Circle -> {circle: Circle}, Square -> {square: " \
           "Square})" in back.to_text(), back.to_text()


def test_a_wrapper_pairs_with_the_schema_it_looks_renamed_to(write):
    def spec(version, options, shapes):
        return HEAD.replace("version: '1'", f"version: '{version}'") + dedent(f"""
            paths:
              /items:
                get:
                  responses:
                    '200':
                      content:
                        application/json:
                          schema: {{oneOf: [{options}]}}
            components:
              schemas:
                Circle: {{properties: {{radius: {{type: number}}}}}}
            """) + "".join(f"    {name}: {{properties: {{a: {{type: number}}, b: {{type: number}}, "
                           f"c: {{type: number}}}}}}\n" for name in shapes)
    ref = "{$ref: '#/components/schemas/%s'}"
    old = spec("1.0", "{properties: {circle: %s}}, {properties: {oval: %s}}" % (ref % "Circle", ref % "Oval"),
               ["Oval"])
    new = spec("2.0", f"{ref % 'Circle'}, {ref % 'Ellipse'}", ["Ellipse"])
    root = write({"a/openapi.yaml": old, "b/openapi.yaml": new})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "1.0")
    s.load(root / "b/openapi.yaml", "2.0")
    text = s.compare("GET /items", "1.0", "2.0").to_text()
    assert "({circle: Circle} -> Circle, {oval: Oval} -> Ellipse (possibly renamed))" in text, text
    assert "option added" not in text and "option removed" not in text
    back = s.compare("GET /items", "2.0", "1.0").to_text()
    assert "(Circle -> {circle: Circle}, Ellipse -> {oval: Oval} (possibly renamed))" in back, back


def test_two_wrappers_of_one_schema_pair_with_one_option(write):
    def spec(version, options):
        return HEAD.replace("version: '1'", f"version: '{version}'") + dedent(f"""
            components:
              schemas:
                Shape: {{oneOf: [{options}]}}
                Circle: {{properties: {{radius: {{type: number}}}}}}
            """)
    ref = "{$ref: '#/components/schemas/Circle'}"
    root = write({"a/openapi.yaml": spec("1.0", f"{{properties: {{circle: {ref}}}}}, {{properties: {{round: {ref}}}}}"),
                  "b/openapi.yaml": spec("2.0", ref)})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "1.0")
    s.load(root / "b/openapi.yaml", "2.0")
    found = [k for k, _, _ in kinds(s.compare("Shape", "1.0", "2.0"))]
    assert found.count("options-unwrapped") == 1 and "option-removed" in found, found


def security_spec(root_security: str, ops: dict[str, str], schemes: str) -> str:
    """A spec with a document-wide `security`, operations that may set their own, and these schemes."""
    paths = "".join(f"  {path}:\n    get:\n      operationId: {op_id}\n{own}      responses:\n"
                    f"        '200': {{description: ok}}\n" for path, (op_id, own) in ops.items())
    return HEAD + (f"security: {root_security}\n" if root_security else "") + "paths:\n" + paths + \
        "components:\n  securitySchemes:\n" + schemes


SCHEMES = """\
    vetAuth:
      type: oauth2
      flows: {clientCredentials: {tokenUrl: 'https://auth.example/token', scopes: {pets:read: r, pets:write: w}}}
    apiKey: {type: apiKey, in: header, name: X-Key}
"""


def test_security_changes_are_rated_for_clients(write):
    old = security_spec("", {
        "/pets": ("listPets", ""),  # nothing declared: anonymous
        "/vets": ("listVets", "      security: [{vetAuth: [pets:read]}, {apiKey: []}]\n"),
        "/owners": ("listOwners", "      security: [{vetAuth: [pets:read, pets:write]}]\n"),
        "/stores": ("stores", "      security: [{apiKey: []}]\n"),
    }, SCHEMES)
    new = security_spec("[{vetAuth: [pets:read]}]", {
        "/pets": ("listPets", ""),  # now the document-wide requirement
        "/vets": ("getVets", "      security: [{vetAuth: [pets:read, pets:write]}]\n"),
        "/owners": ("listOwners", "      security: [{vetAuth: [pets:read]}, {}]\n"),
        "/stores": ("stores", "      security: [{apiKey: []}]\n"),
    }, SCHEMES.replace("in: header, name: X-Key", "in: query, name: key"))
    root = write({"a/openapi.yaml": old, "b/openapi.yaml": new})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    assert kinds(s.compare("GET /pets", "a", "b")) == {("anonymous-access-removed", "security vetAuth", "breaking")}
    assert kinds(s.compare("GET /vets", "a", "b")) == {
        ("operation-id-changed", "operationId", "info"),
        ("security-removed", "security apiKey", "breaking"),
        ("scope-added", "security vetAuth", "breaking")}
    assert kinds(s.compare("GET /owners", "a", "b")) == {
        ("anonymous-access-added", "security", "info"), ("scope-removed", "security vetAuth", "info")}
    # The scheme itself changed where the key goes: every operation using it breaks.
    stores = s.compare("GET /stores", "a", "b")
    assert kinds(stores) == {("security-scheme-changed", "security apiKey", "breaking")}
    assert stores.changes[0].before == "type apiKey; in header; name X-Key"
    vets = {c.kind: c for c in s.compare("GET /vets", "a", "b").changes}
    assert (vets["operation-id-changed"].before, vets["operation-id-changed"].after) == ("listVets", "getVets")
    assert (vets["scope-added"].before, vets["scope-added"].after) == ("pets:read", "pets:read, pets:write")
    d = s.diff("a", "b")
    assert {op for op, _ in d.breaking()} == {"GET /pets", "GET /vets", "GET /stores"}


def test_security_schemes_added_and_removed_and_empty_security_is_anonymous(write):
    extra = "    vetToken: {type: http, scheme: bearer}\n"
    old = security_spec("", {"/pets": ("listPets", "      security: []\n")}, SCHEMES)
    new = security_spec("", {"/pets": ("listPets", "")}, SCHEMES.split("    apiKey")[0] + extra)
    root = write({"a/openapi.yaml": old, "b/openapi.yaml": new})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    d = s.diff("a", "b")
    assert d.operations_changed == {}  # `security: []` and nothing declared both allow anonymous access
    assert (d.security_schemes_added, d.security_schemes_removed) == (["vetToken"], ["apiKey"])
    assert "security schemes added: vetToken" in d.to_text()


def test_operations_show_their_security_and_undefined_schemes_are_reported(write):
    from speclens import verify
    old = security_spec("", {"/pets": ("listPets", "")}, SCHEMES)
    new = security_spec("[{vetAuth: [pets:read]}, {}]", {
        "/pets": ("listPets", ""),
        "/vets": ("listVets", "      security: [{vetAuth: [pets:read]}, {apiKey: []}, {vetCert: []}]\n")},
        SCHEMES)
    root = write({"a/openapi.yaml": old, "b/openapi.yaml": new})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    text = s.get_operation("GET /vets", label="b").to_text()
    assert "security: vetAuth (oauth2; scopes pets:read) or apiKey (apiKey) or vetCert (undefined)" in text
    assert "security: vetAuth (oauth2; scopes pets:read) or anonymous" in s.get_operation("GET /pets", label="b").to_text()
    assert "security: none declared" in s.get_operation("GET /pets", label="a").to_text()
    # A scheme no requirement names says nothing is needed: worth a warning.
    unused = [i.message for i in s.specs["a"].issues if i.code == "unused-security-scheme"]
    assert len(unused) == 2 and "'apiKey'" in unused[0] and "'vetAuth'" in unused[1]
    issues = [i for i in s.specs["b"].issues if i.code == "unknown-security-scheme"]
    assert [(i.file, "vetCert" in i.message) for i in issues] == [("openapi.yaml", True)] and issues[0].line
    assert kinds(s.compare("GET /pets", "a", "b")) == {("security-added", "security vetAuth", "info")}


def test_cover_counts_naming_the_scheme_as_mentioning_a_security_change(write):
    from speclens import verify
    old = security_spec("", {"/pets": ("listPets", "")}, SCHEMES)
    new = security_spec("[{vetAuth: [pets:read]}]", {"/pets": ("listPets", "")}, SCHEMES)
    root = write({"a/openapi.yaml": old, "b/openapi.yaml": new})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    c = s.compare("GET /pets", "a", "b")
    assert verify("GET /pets now needs a `vetAuth` token.", [c], cover=[c]).ok
    r = verify("GET /pets is unchanged apart from its description.", [c], cover=[c])
    assert [f.kind for f in r.findings] == ["omitted-change"] and "anonymous access removed" in r.findings[0].text


def test_a_diff_says_what_looks_renamed_first_and_lists_every_change_in_full(write):
    pet = """
        paths:
          /pets:
            get:
              responses:
                '200': {description: ok, content: {application/json: {schema: {$ref: '#/components/schemas/Pet'}}}}
        components:
          schemas:
            Pet:
              required: [%s]
              properties:
                %s: {type: string}
                %s: {type: string, format: date-time}
                note: {type: string, maxLength: %d}
        """
    root = write({"a/openapi.yaml": HEAD + dedent(pet % ("callSign", "callSign", "eta", 40)),
                  "b/openapi.yaml": HEAD + dedent(pet % ("callsign", "callsign", "arrival", 80))})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    d = s.diff("a", "b")
    # Only case or separators differ: a likely rename. A new name is a likely rename only when it is the only
    # field gone and the only one added, of the same type.
    assert d.possible_field_renames == [
        {"schema": "Pet", "from": "callSign", "to": "callsign", "why": "the same name but for case or separators"},
        {"schema": "Pet", "from": "eta", "to": "arrival",
         "why": "the only field removed and the only one added, both string (date-time)"}]
    lines = d.to_text().splitlines()
    rename = "possibly renamed: field Pet.callSign -> callsign (the same name but for case or separators)"
    assert lines.index(rename) < next(i for i, x in enumerate(lines) if x.startswith("breaking:"))
    # Changes that break nothing are counted in the default text, and listed in full.
    assert any(x.startswith("other changes:") for x in lines)
    full = d.to_text(full=True)
    assert "info: field added: arrival" in full and "info: field removed: eta" in full
    assert "field added: arrival" not in d.to_text()
    # Comparing the schema gives the same hints, each with the two fields it pairs, beside the field removed
    # and the field added.
    hints = [(c.before, c.after, c.a, c.b, c.level, c.note) for c in s.compare("Pet", "a", "b").changes
             if c.kind == "possibly-renamed"]
    assert hints == [("callSign", "callsign", "a:field/Pet.callSign", "b:field/Pet.callsign", "info",
                      "the same name but for case or separators"),
                     ("eta", "arrival", "a:field/Pet.eta", "b:field/Pet.arrival", "info",
                      "the only field removed and the only one added, both string (date-time)")]
    text = s.compare("Pet", "a", "b").to_text()
    assert "info: possibly renamed (eta -> arrival) [b:field/Pet.arrival]" in text and "field removed: eta" in text
    # A field compared on its own is not paired with anything.
    assert all(c.kind != "possibly-renamed" for c in s.compare("Pet.eta", "a", "b").changes)


def test_a_diff_lists_a_shared_breaking_change_once_and_guesses_only_lone_renames(write):
    spec = """
        paths:
          /boxes:
            get:
              parameters: [{name: %s, in: query, schema: {type: %s}}]
              responses: {'200': {description: ok, content: {application/json: {schema: {$ref: '#/components/schemas/Box'}}}}}
          /boxes/{id}:
            get:
              parameters: [{name: id, in: path, required: true, schema: {type: string}}]
              responses: {'200': {description: ok, content: {application/json: {schema: {$ref: '#/components/schemas/Box'}}}}}
          /crates:
            get:
              parameters: [{name: %s, in: query, schema: {type: %s}}]
              responses: {'200': {description: ok, content: {application/json: {schema: {$ref: '#/components/schemas/Crate'}}}}}
        components:
          schemas:
            Box:
              properties:
                size: {type: string, enum: %s}
                %s: {type: string}
            Crate:
              properties:
                %s: {type: integer}
                %s: {type: integer}
        """
    root = write({"a/openapi.yaml": HEAD + dedent(spec % ("page", "integer", "page", "integer", "[s, m]", "label",
                                                          "width", "depth")),
                  "b/openapi.yaml": HEAD + dedent(spec % ("cursor", "string", "cursor", "string", "[s, m, l]",
                                                          "tag", "breadth", "height"))})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    d = s.diff("a", "b")
    # The only field gone and the only one added, of one type: a likely rename. Two gone and two added: no guess.
    assert [(r["schema"], r["from"], r["to"]) for r in d.possible_field_renames] == [("Box", "label", "tag")]
    # The only query parameter gone and the only one added, in each operation: one hint for both.
    assert d.possible_parameter_renames == [{
        "in": "query", "from": "page", "to": "cursor", "operations": ["GET /boxes", "GET /crates"],
        "why": "the only query parameter removed and the only one added, integer -> string"}]
    text = d.to_text()
    assert "possibly renamed: query parameter page -> cursor in 2 operations" in text
    # A value added to a shared enum breaks two operations: said once, with the other operation.
    assert text.count("enum value added") == 1
    assert "  breaking: enum value added: GET /boxes response 200 application/json: size (none -> 'l')" in text
    assert "\n    also in 1 operation: GET /boxes/{id}\n" in text


def test_the_same_bound_or_value_written_another_way_is_no_change(write):
    old = dedent("""
        openapi: 3.0.3
        info: {title: t, version: '1'}
        paths: {}
        components:
          schemas:
            Ring:
              properties:
                spin: {type: number, maximum: 1.5, exclusiveMaximum: true}
                tilt: {type: number, minimum: 0, exclusiveMinimum: true, maximum: 90}
                kind: {type: string, enum: [ring]}
                load: {type: number, maximum: 10, exclusiveMaximum: true}
                mode: {type: string, enum: [a]}
        """)
    new = dedent("""
        openapi: 3.1.0
        info: {title: t, version: '2'}
        paths: {}
        components:
          schemas:
            Ring:
              properties:
                spin: {type: number, exclusiveMaximum: 1.5}
                tilt: {type: number, exclusiveMinimum: 0, maximum: 90}
                kind: {type: string, const: ring}
                load: {type: number, maximum: 10}
                mode: {type: string, const: b}
        """)
    root = write({"a/openapi.yaml": old, "b/openapi.yaml": new,
                  "c/openapi.yaml": new.replace("exclusiveMaximum: 1.5", "exclusiveMaximum: 1.2")})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    s.load(root / "c/openapi.yaml", "c")
    # A change to a 3.1 exclusive bound is a change.
    [spin] = s.compare("Ring.spin", "b", "c").changes
    assert (spin.kind, spin.before, spin.after) == ("constraint-tightened", "exclusiveMaximum 1.5", "exclusiveMaximum 1.2")
    # OpenAPI 3.0's boolean exclusive bound and 3.1's numeric one are the same bound; so are enum [x] and const x.
    for name in ("Ring.spin", "Ring.tilt", "Ring.kind"):
        assert s.compare(name, "a", "b").changes == [], name
    # The same value, no longer exclusive: one more value allowed.
    [load] = s.compare("Ring.load", "a", "b").changes
    assert (load.kind, load.before, load.after) == ("constraint-loosened", "exclusiveMaximum 10", "maximum 10")
    assert {(c.kind, c.before, c.after) for c in s.compare("Ring.mode", "a", "b").changes} == {
        ("enum-value-added", None, "'b'"), ("enum-value-removed", "'a'", None)}


def test_a_field_renamed_on_a_parent_is_one_rename_naming_who_inherits_it(write):
    spec = """
        paths: {}
        components:
          schemas:
            Animal:
              properties:
                %s: {type: string}
            Cat:
              allOf: [{$ref: '#/components/schemas/Animal'}, {properties: {lives: {type: integer}}}]
            Dog:
              allOf: [{$ref: '#/components/schemas/Animal'}]
        """
    root = write({"a/openapi.yaml": HEAD + dedent(spec % "nickName"),
                  "b/openapi.yaml": HEAD + dedent(spec % "nickname")})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    d = s.diff("a", "b")
    # Said once, where the field is written; the schemas that inherit it are named, not given renames of their own.
    assert d.possible_field_renames == [
        {"schema": "Animal", "from": "nickName", "to": "nickname",
         "why": "the same name but for case or separators", "inherited_by": ["Cat", "Dog"]}]
    assert ("possibly renamed: field Animal.nickName -> nickname (inherited by Cat, Dog) "
            "(the same name but for case or separators)") in d.to_text().splitlines()


def test_a_renamed_field_now_written_elsewhere_is_said_once_where_it_was_written(write):
    spec = """
        paths: {}
        components:
          schemas:
            Base:
              properties:
                %s
            Zebra:
              %s
              properties:
                %s
            Ant:
              allOf: [%s]
        """
    # Zebra writes `spot` and Ant inherits it; then both inherit `spots` from Base.
    old = spec % ("id: {type: string}", "", "spot: {type: string}", "{$ref: '#/components/schemas/Zebra'}")
    new = spec % ("spots: {type: string}", "allOf: [{$ref: '#/components/schemas/Base'}]", "{}",
                  "{$ref: '#/components/schemas/Zebra'}")
    root = write({"a/openapi.yaml": HEAD + dedent(old), "b/openapi.yaml": HEAD + dedent(new)})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    renames = [r for r in s.diff("a", "b").possible_field_renames if r["from"] == "spot"]
    assert [(r["schema"], r.get("inherited_by")) for r in renames] == [("Zebra", ["Ant"])]


def test_what_a_field_says_about_itself_is_one_change_when_its_type_is_written_inline(write):
    spec = """
        paths: {}
        components:
          schemas:
            Box:
              properties:
                tags: {type: array, description: %s, items: {type: string}}
                dims: {type: object, description: %s, properties: {w: {type: integer}}}
        """
    root = write({"a/openapi.yaml": HEAD + dedent(spec % ("Old.", "Old.")),
                  "b/openapi.yaml": HEAD + dedent(spec.replace("description: %s, items",
                                                               "description: %s, deprecated: true, items")
                                                  % ("New.", "New."))})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    r = s.compare("Box", "a", "b")
    # The field and its inline type are one object in the spec: each change once, cited on the field.
    assert sorted((c.kind, c.where) for c in r.changes) == [
        ("deprecated", "tags"), ("description-changed", "dims"), ("description-changed", "tags")]
    assert "schema/Box.tags]" not in r.to_text()


def test_what_a_field_says_about_itself_is_reported_on_every_way_to_it(write):
    spec = """
        paths:
          /x:
            get:
              parameters: [{name: q, in: query, schema: {$ref: '#/components/schemas/Foo/properties/bar'}}]
              responses:
                '200': {description: ok, content: {application/json: {schema: {$ref: '#/components/schemas/Foo/properties/bar'}}}}
        components:
          schemas:
            Foo:
              properties:
                bar: {type: array, description: %s, %s items: {type: string, description: %s}}
            X:
              properties:
                baz: {$ref: '#/components/schemas/Foo/properties/bar'}
            Pet:
              description: %s
              %s
              properties: {name: {type: string}}
            Box:
              properties:
                tags: %s
                kept: %s
            Tags: {type: array, items: {type: string}}
            Kept: {type: array, description: Kept., deprecated: true, items: {type: string}}
        """
    old = spec % ("Old.", "", "A tag.", "A pet.", "", "{type: array, description: Old., deprecated: true, items: {type: string}}",
                  "{type: array, description: Kept., deprecated: true, items: {type: string}}")
    new = spec % ("New.", "deprecated: true,", "A label.", "A cat.", "deprecated: true",
                  "{$ref: '#/components/schemas/Tags', description: New.}", "{$ref: '#/components/schemas/Kept'}")
    root = write({"a/openapi.yaml": HEAD + dedent(old), "b/openapi.yaml": HEAD + dedent(new)})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")

    def kinds(name):
        return sorted((c.kind, c.where) for c in s.compare(name, "a", "b").changes)

    # By the field's dotted name, by a field, a parameter and a response that point into it: never lost.
    assert kinds("Foo.bar") == [("deprecated", ""), ("description-changed", ""), ("description-changed", "[]")]
    assert ("deprecated", "baz") in kinds("X") and ("description-changed", "baz") in kinds("X")
    assert {("deprecated", "parameter query q"), ("deprecated", "response 200 application/json")} <= set(kinds("GET /x"))
    assert "GET /x" in s.diff("a", "b").operations_changed
    # On the owning schema, once each, the inline items' own description too.
    assert kinds("Foo") == [("deprecated", "bar"), ("description-changed", "bar"), ("description-changed", "bar[]")]
    # A named schema's own words.
    assert kinds("Pet") == [("deprecated", ""), ("description-changed", "")]
    # Inline on one side, a $ref on the other: what applies, once. Moved unchanged into a schema: nothing.
    assert kinds("Box") == [("description-changed", "tags"), ("undeprecated", "tags")]


def test_a_fields_dotted_name_is_the_field_whether_its_type_is_inline_or_a_schema(write):
    spec = """
        paths: {}
        components:
          schemas:
            Box:
              properties:
                tags: %s
                dims: {type: object, description: %s, properties: {w: {type: integer}}}
            Crate:
              allOf: [{$ref: '#/components/schemas/Box'}]
            Tags: {type: array, description: Old., items: {type: string}}
            v1.Lid: {type: object, description: %s}
        """
    root = write({"a/openapi.yaml": HEAD + dedent(spec % ("{type: array, description: Old., items: {type: string}}",
                                                          "Old.", "Old.")),
                  "b/openapi.yaml": HEAD + dedent(spec % ("{$ref: '#/components/schemas/Tags'}", "New.", "New."))})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    # Its inline type moved into a schema of its own, the same words: not "schema removed", and no change.
    for name in ("Box.tags", "Crate.tags"):
        r = s.compare(name, "a", "b")
        assert (r.what, r.changes) == ("field", [])
    # Inline on both sides: the field, by its own name or through inheritance alike.
    for name in ("Box.dims", "Crate.dims"):
        r = s.compare(name, "a", "b")
        assert r.what == "field" and [(c.kind, c.b) for c in r.changes] == [("description-changed", "b:field/Box.dims")]
    # A schema whose name has a dot is still that schema.
    r = s.compare("v1.Lid", "a", "b")
    assert r.what == "schema" and [c.kind for c in r.changes] == ["description-changed"]


def test_a_default_is_compared_as_the_json_value_it_is(write):
    spec = """
        paths: {}
        components:
          schemas:
            Gauge:
              properties:
                level: {type: number, default: .nan}
                on: {default: %s}
                range: {type: array, default: [0, .nan]}
        """
    root = write({"a/openapi.yaml": HEAD + dedent(spec % "1"), "b/openapi.yaml": HEAD + dedent(spec % "true")})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    # NaN on both sides is the same default; 1 and true are different values.
    assert [(c.kind, c.where, c.before, c.after) for c in s.compare("Gauge", "a", "b").changes] == [
        ("default-changed", "on", "1", "True")]


def test_a_schema_kept_under_an_extension_key_is_named_like_a_component(write):
    spec = """
        paths:
          /box:
            get:
              responses:
                '200': {description: ok, content: {application/json: {schema: {$ref: '#/x-schemas/Box'}}}}
        x-schemas:
          Box:
            properties:
              tags: {type: array, description: %s, items: {type: string}}
              dims: {type: object, description: %s, properties: {w: {type: integer}}}
        """
    # Two versions in files named differently.
    root = write({"v1.yaml": HEAD + dedent(spec % ("Old.", "Old.")), "v2.yaml": HEAD + dedent(spec % ("New.", "New."))})
    s = SpecSet()
    s.load(root / "v1.yaml", "a")
    s.load(root / "v2.yaml", "b")
    assert "b:field/Box.tags" in s.get("b").graph.nodes
    # Its fields' inline types are part of it, not schemas of their own; and it is the same schema in both files.
    d = s.diff("a", "b")
    assert (d.schemas_added, d.schemas_removed, sorted(d.schemas_changed)) == ([], [], ["Box"])
    assert [(c.kind, c.where) for c in s.compare("Box", "a", "b").changes] == [
        ("description-changed", "tags"), ("description-changed", "dims")]
    assert [(c.kind, c.where) for c in s.compare("GET /box", "a", "b").changes] == [
        ("description-changed", "response 200 application/json: tags"),
        ("description-changed", "response 200 application/json: dims")]


def _schema_pair(write, a: str, b: str):
    spec = """
        paths:
          /x:
            post:
              requestBody: {content: {application/json: {schema: {$ref: '#/components/schemas/X'}}}}
              responses: {'200': {description: ok, content: {application/json: {schema: {$ref: '#/components/schemas/X'}}}}}
        components:
          schemas:
            X: %s
        """
    root = write({"a/openapi.yaml": HEAD + dedent(spec % a), "b/openapi.yaml": HEAD + dedent(spec % b)})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    return [(c.kind, c.before, c.after) for c in s.compare("X", "a", "b").changes]


def test_additional_properties_from_anything_to_a_schema_to_nothing(write):
    obj = "{type: object, properties: {n: {type: string}}%s}"
    cases = {
        ("", ", additionalProperties: true"): [],  # absent and true allow the same
        ("", ", additionalProperties: false"): [("constraint-tightened", "additionalProperties: anything",
                                                 "additionalProperties: false")],
        ("", ", additionalProperties: {type: string}"): [("constraint-tightened", "additionalProperties: anything",
                                                          "additionalProperties: a schema")],
        (", additionalProperties: {type: string}", ", additionalProperties: false"): [
            ("constraint-tightened", "additionalProperties: a schema", "additionalProperties: false")],
        (", additionalProperties: false", ", additionalProperties: {type: string}"): [
            ("constraint-loosened", "additionalProperties: false", "additionalProperties: a schema")],
        (", additionalProperties: false", ""): [
            ("constraint-loosened", "additionalProperties: false", "additionalProperties: anything")],
        (", additionalProperties: {type: string}", ", additionalProperties: {type: integer}"): [
            ("type-changed", "string", "integer")],
    }
    for (a, b), want in cases.items():
        assert _schema_pair(write, obj % a, obj % b) == want, (a, b)


def test_items_unique_items_and_multiple_of_are_compared(write):
    cases = {
        ("{type: array}", "{type: array, items: {type: integer}}"): [
            ("constraint-tightened", "no items", "items: a schema")],
        ("{type: array, items: {type: integer}}", "{type: array}"): [
            ("constraint-loosened", "items: a schema", "no items")],
        ("{type: array, items: {type: string}}", "{type: array, uniqueItems: true, items: {type: string}}"): [
            ("constraint-tightened", "uniqueItems false", "uniqueItems true")],
        ("{type: number, multipleOf: 0.1}", "{type: number, multipleOf: 0.01}"): [
            ("constraint-loosened", "multipleOf 0.1", "multipleOf 0.01")],
        ("{type: number, multipleOf: 0.01}", "{type: number, multipleOf: 0.1}"): [
            ("constraint-tightened", "multipleOf 0.01", "multipleOf 0.1")],
        ("{type: integer, multipleOf: 2}", "{type: integer, multipleOf: 3}"): [
            ("multiple-of-changed", "multipleOf 2", "multipleOf 3")],
        ("{type: integer}", "{type: integer, multipleOf: 5}"): [
            ("constraint-tightened", "multipleOf none", "multipleOf 5")],
        ("{type: array, prefixItems: [{type: string}]}", "{type: array, prefixItems: [{type: string}, {type: integer}]}"): [
            ("prefix-items-changed", "1 position", "2 positions")],
    }
    for (a, b), want in cases.items():
        assert _schema_pair(write, a, b) == want, (a, b)


def test_a_request_body_made_optional_and_a_parameter_written_otherwise(write):
    op = """
        paths:
          /x:
            post:
              parameters:
                - {name: ids, in: query, %s schema: {type: array, items: {type: string}}}
                - {name: page, in: query, schema: {type: integer}}
              requestBody: {required: %s, content: {application/json: {schema: {type: object}}}}
              responses: {'200': {description: ok}}
        """
    root = write({"a/openapi.yaml": HEAD + dedent(op % ("", "true")),
                  "b/openapi.yaml": HEAD + dedent(op % ("explode: false,", "false"))})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    assert sorted((c.kind, c.where, c.before, c.after, c.level) for c in s.compare("POST /x", "a", "b").changes) == [
        ("parameter-serialization-changed", "parameter query ids", "style form, explode true",
         "style form, explode false", "breaking"),
        ("required-removed", "request body", None, None, "info")]


def test_items_or_values_on_one_side_of_a_field_written_inline(write):
    field = "{type: object, properties: {n: %s}}"
    assert _schema_pair(write, field % "{type: array}", field % "{type: array, items: {type: integer}}") == [
        ("constraint-tightened", "no items", "items: a schema")]
    assert _schema_pair(write, field % "{type: object, additionalProperties: {type: string}}",
                        field % "{type: object}") == [
        ("constraint-loosened", "additionalProperties: a schema", "additionalProperties: anything")]


def test_a_single_value_is_sent_the_same_whatever_explode_says(write):
    op = """
        paths:
          /x:
            post:
              parameters:
                - {name: limit, in: query, %s schema: {type: integer}}
                - {name: trace, in: header, %s schema: {type: integer}}
                - {name: ids, in: query, %s schema: {type: array, items: {type: integer}}}
                - {name: id, in: path, required: true, %s schema: {$ref: '#/components/schemas/Id'}}
                - {name: sort, in: query, %s schema: {type: string}}
              responses: {'200': {description: ok}}
        components:
          schemas:
            Id: {type: integer}
        """
    root = write({"a/openapi.yaml": HEAD + dedent(op % ("", "", "", "", "")),
                  "b/openapi.yaml": HEAD + dedent(op % ("explode: false,", "explode: true,", "explode: false,",
                                                         "style: label,", "style: deepObject,"))})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    assert sorted((c.kind, c.where, c.before, c.after, c.level) for c in s.compare("POST /x", "a", "b").changes) == [
        ("parameter-serialization-changed", "parameter path id", "style simple, explode false",
         "style label, explode false", "breaking"),
        ("parameter-serialization-changed", "parameter query ids", "style form, explode true",
         "style form, explode false", "breaking"),
        ("parameter-style-undefined", "parameter query sort", "style form, explode true",
         "style deepObject, explode false", "warning")]


def test_not_if_then_else_and_dependent_schemas_are_compared(write):
    obj = "{type: object, properties: {kind: {type: string}, n: {type: integer}}%s}"
    cond = ", if: {properties: {kind: {const: a}}}, then: {required: [n]}"
    cases = {
        ("", cond): [("constraint-tightened", "no then", "then: a schema")],
        (cond, ""): [("constraint-loosened", "then: a schema", "no then")],
        (cond, cond + ", else: {required: [kind]}"): [("constraint-tightened", "no else", "else: a schema")],
        (", then: {required: [n]}", cond): [("condition-changed", "no if", "if: a schema")],
        (cond, cond.replace("const: a", "const: b")): [("condition-changed", "if: a schema", "if: another")],
        (cond, ", if: {properties: {kind: {const: a}}, description: test}, then: {required: [n]}"): [],
        (cond.replace("{required: [n]}", "{properties: {n: {maximum: 5}}}"),
         cond.replace("{required: [n]}", "{properties: {n: {maximum: 4}}}")): [
            ("constraint-tightened", "maximum 5", "maximum 4")],
        ("", ", not: {required: [n]}"): [("constraint-tightened", "no not", "not: a schema")],
        (", not: {required: [n]}", ""): [("constraint-loosened", "not: a schema", "no not")],
        (", not: {required: [n]}", ", not: {required: [kind]}"): [
            ("condition-changed", "not: a schema", "not: another")],
        ("", ", dependentSchemas: {kind: {required: [n]}}"): [
            ("constraint-tightened", "no dependentSchemas/kind", "dependentSchemas/kind: a schema")],
        (", dependentSchemas: {kind: {properties: {n: {minimum: 0}}}}",
         ", dependentSchemas: {kind: {properties: {n: {minimum: 1}}}}"): [
            ("constraint-tightened", "minimum 0", "minimum 1")],
        (", dependentSchemas: {kind: {required: [n]}}", ", dependentSchemas: {n: {required: [n]}}"): [
            ("constraint-loosened", "dependentSchemas/kind: a schema", "no dependentSchemas/kind"),
            ("constraint-tightened", "no dependentSchemas/n", "dependentSchemas/n: a schema")],
        # written as booleans: `then: true` and `not: false` do nothing; `then: false` and `not: true` allow nothing
        (", then: true", ""): [],
        ("", ", not: false"): [],
        ("", ", not: true"): [("constraint-tightened", "no not", "not: true")],
        (", not: {required: [n]}", ", not: true"): [("constraint-tightened", "not: a schema", "not: true")],
        (cond, cond.replace("{required: [n]}", "false")): [("constraint-tightened", "then: a schema", "then: false")],
        (", dependentSchemas: {kind: false}", ", dependentSchemas: {kind: true}"): [
            ("constraint-loosened", "dependentSchemas/kind: false", "dependentSchemas/kind: true")],
        (cond, cond.replace("if: {properties: {kind: {const: a}}}", "if: true")): [
            ("condition-changed", "if: a schema", "if: true")],
    }
    for (a, b), want in cases.items():
        assert _schema_pair(write, obj % a, obj % b) == want, (a, b)


def test_items_false_is_a_constraint(write):
    arr = "{type: array, prefixItems: [{type: string}]%s}"
    assert _schema_pair(write, arr % "", arr % ", items: false") == [
        ("constraint-tightened", "items: anything", "items: false")]
    assert _schema_pair(write, arr % ", items: false", arr % ", items: {type: string}") == [
        ("constraint-loosened", "items: false", "items: a schema")]


_SILENT = [  # (keyword written on a schema, and another value for it): each must give a line when added,
    # removed or changed
    ("not: {type: string}", "not: {type: integer}"),
    ("if: {required: [a]}, then: {required: [b]}", "if: {required: [b]}, then: {required: [b]}"),
    ("if: {required: [a]}, then: {required: [b]}", "if: {required: [a]}, then: {required: [a]}"),
    ("if: {required: [a]}, else: {required: [b]}", "if: {required: [a]}, else: {required: [a]}"),
    ("dependentSchemas: {a: {required: [b]}}", "dependentSchemas: {a: {required: [a]}}"),
    ("not: true", "not: {type: string}"),
    ("if: true, then: false", "if: false, then: false"),
    ("dependentSchemas: {a: false}", "dependentSchemas: {a: {required: [a]}}"),
    ("items: {type: string}", "items: {type: integer}"),
    ("items: false", "items: {type: integer}"),
    ("prefixItems: [{type: string}]", "prefixItems: [{type: integer}]"),
    ("contains: {type: string}", "contains: {type: integer}"),
    ("unevaluatedItems: {type: string}", "unevaluatedItems: false"),
    ("additionalProperties: {type: string}", "additionalProperties: false"),
    ("unevaluatedProperties: {type: string}", "unevaluatedProperties: false"),
    ("additionalProperties: false", "additionalProperties: {type: string}"),
    ("propertyNames: {maxLength: 3}", "propertyNames: {maxLength: 4}"),
    ("patternProperties: {'^x': {type: string}}", "patternProperties: {'^x': {type: integer}}"),
    ("multipleOf: 2", "multipleOf: 3"),
    ("uniqueItems: true", "uniqueItems: false"),
    ("const: 1", "const: 2"),
    ("enum: [1]", "enum: [2]"),
    ("pattern: '^a'", "pattern: '^b'"),
    ("format: date", "format: uuid"),
    ("maxLength: 3", "maxLength: 4"),
    ("minItems: 1", "minItems: 2"),
    ("maxProperties: 1", "maxProperties: 2"),
    ("required: [a]", "required: [b]"),
]
# Not yet read: a field written inline with only `required: [names]` keeps the field's own required flag instead.
_NOT_READ = {("{type: object, properties: {f: {%s}}}", "required: [a]")}


def test_no_keyword_read_into_the_graph_changes_silently(write):
    for x, y in _SILENT:
        for where in ("{%s}", "{type: object, properties: {f: {%s}}}", "{type: array, items: {%s}}"):
            if (where, x) in _NOT_READ:
                continue
            for a, b in ((x, ""), ("", x), (x, y)):
                assert _schema_pair(write, where % a, where % b), (where, a, b)


def test_a_requirement_is_compared_whether_or_not_its_name_is_declared(write):
    spec = """
        paths:
          /x:
            get:
              responses: {'200': {description: ok, content: {application/json: {schema: {$ref: '#/components/schemas/X'}}}}}
        components:
          schemas:
            X: {type: object, properties: {n: {type: string}}, dependentSchemas: {n: %s}}
        """

    def changes(a, b):
        root = write({"a/openapi.yaml": HEAD + dedent(spec % a), "b/openapi.yaml": HEAD + dedent(spec % b)})
        s = SpecSet()
        s.load(root / "a/openapi.yaml", "a")
        s.load(root / "b/openapi.yaml", "b")
        return sorted((c.kind, c.where, c.level) for c in s.compare("GET /x", "a", "b").changes)

    undeclared = "{required: [kind]}"
    optional = "{properties: {kind: {maxLength: 3}}}"
    required = "{properties: {kind: {maxLength: 3}}, required: [kind]}"
    where = "response 200 application/json: dependentSchemas/n.kind"
    assert changes(undeclared, optional) == [("field-added", where, "info"), ("required-removed", where, "breaking")]
    assert changes(optional, undeclared) == [("field-removed", where, "info"), ("required-added", where, "info")]
    assert changes(undeclared, required) == [("required-field-added", where, "info")]  # said once
    assert changes("{type: object}", required) == [("required-field-added", where, "info")]
    assert changes(required, "{type: object}") == [("required-field-removed", where, "breaking")]


def test_const_and_enum_values_are_compared_as_the_json_values_they_are(write):
    spec = """
        paths: {}
        components:
          schemas:
            Flag:
              properties:
                one: {const: %s}
                some: {enum: %s}
                same: {enum: [.nan, 2]}
                list: {const: [0, %s]}
        """
    root = write({"a/openapi.yaml": HEAD + dedent(spec % ("1", "[1, 2]", "1")),
                  "b/openapi.yaml": HEAD + dedent(spec % ("true", "[true, 2]", "true"))})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    # 1 and true are different values; NaN is the same as NaN.
    assert sorted((c.kind, c.where, c.before, c.after) for c in s.compare("Flag", "a", "b").changes) == [
        ("const-changed", "list", "[0, 1]", "[0, True]"), ("const-changed", "one", "1", "True"),
        ("enum-value-added", "some", None, "True"), ("enum-value-removed", "some", "1", None)]


PATHS = """
    paths:
%s"""
OP = """      /pets/{%s}/toys/{%s}:
        get:
          parameters:
            - {name: %s, in: path, required: true, schema: {type: string%s}}
            - {name: %s, in: path, required: true, schema: {type: integer}}
          responses: {'200': {description: ok}}
"""


def _two(write, a: str, b: str):
    root = write({"a/openapi.yaml": HEAD + dedent(PATHS % a), "b/openapi.yaml": HEAD + dedent(PATHS % b)})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    return s


def test_a_path_parameter_renamed_is_the_same_operation(write):
    s = _two(write, OP % ("id", "toy", "id", "", "toy"), OP % ("petId", "toy", "petId", ", maxLength: 5", "toy"))
    d = s.diff("a", "b")
    # Not removed and added: one operation, under its newer name, whose parameter is still compared.
    assert (d.operations_added, d.operations_removed, list(d.operations_changed)) == (
        [], [], ["GET /pets/{petId}/toys/{toy}"])
    want = [("constraint-tightened", "parameter path petId", "maxLength none", "maxLength 5", "breaking"),
            ("path-parameter-renamed", "parameter path petId", "id", "petId", "info")]
    assert sorted((c.kind, c.where, c.before, c.after, c.level)
                  for c in d.operations_changed["GET /pets/{petId}/toys/{toy}"]) == want
    # Asked by either name, the same comparison.
    for name in ("GET /pets/{id}/toys/{toy}", "GET /pets/{petId}/toys/{toy}"):
        r = s.compare(name, "a", "b")
        assert (r.name, r.a, r.b) == ("GET /pets/{petId}/toys/{toy}", "a:operation/GET /pets/{id}/toys/{toy}",
                                      "b:operation/GET /pets/{petId}/toys/{toy}")
        assert sorted((c.kind, c.where, c.before, c.after, c.level) for c in r.changes) == want
    renamed = next(c for c in r.changes if c.kind == "path-parameter-renamed")
    assert (renamed.a, renamed.b) == ("a:parameter/GET /pets/{id}/toys/{toy}#path/id",
                                      "b:parameter/GET /pets/{petId}/toys/{toy}#path/petId")


def test_operations_are_paired_by_path_shape_only_when_one_on_each_side_is_left(write):
    a = OP % ("id", "toy", "id", "", "toy") + OP % ("name", "toy", "name", "", "toy")
    s = _two(write, a, OP % ("petId", "toy", "petId", "", "toy"))
    d = s.diff("a", "b")
    # Two of that shape before, one after: which one it is cannot be told, so none is paired.
    assert (d.operations_added, d.operations_removed) == (
        ["GET /pets/{petId}/toys/{toy}"], ["GET /pets/{id}/toys/{toy}", "GET /pets/{name}/toys/{toy}"])
    # The same name is matched first: the one left on each side is then paired.
    s = _two(write, a, OP % ("id", "toy", "id", "", "toy") + OP % ("petName", "toy", "petName", "", "toy"))
    d = s.diff("a", "b")
    assert (d.operations_added, d.operations_removed, list(d.operations_changed)) == (
        [], [], ["GET /pets/{petName}/toys/{toy}"])


def test_in_a_paired_operation_path_parameters_are_matched_by_their_place(write):
    def op(path, *params):
        lines = "".join(f"            - {{name: {n}, in: path, required: true, schema: {{type: {t}}}}}\n"
                        for n, t in params)
        return f"      {path}:\n        get:\n          parameters:\n{lines}          responses: {{'200': {{description: ok}}}}\n"

    def changes(a, b):
        r = _two(write, a, b).diff("a", "b")
        assert (r.operations_added, r.operations_removed) == ([], [])
        [cs] = r.operations_changed.values()
        return sorted((c.kind, c.where, c.before, c.after, c.level) for c in cs)

    # Names swapped: each place is compared with itself, so its type changes are said.
    assert changes(op("/a/{x}/b/{y}", ("x", "integer"), ("y", "string")),
                   op("/a/{y}/b/{x}", ("x", "integer"), ("y", "string"))) == [
        ("path-parameter-renamed", "parameter path x", "y", "x", "info"),
        ("path-parameter-renamed", "parameter path y", "x", "y", "info"),
        ("type-changed", "parameter path x", "string", "integer", "breaking"),
        ("type-widened", "parameter path y", "integer", "string", "info")]
    # A name moved along: two renames, nothing else changed.
    assert changes(op("/a/{x}/b/{y}", ("x", "integer"), ("y", "string")),
                   op("/a/{y}/b/{z}", ("y", "integer"), ("z", "string"))) == [
        ("path-parameter-renamed", "parameter path y", "x", "y", "info"),
        ("path-parameter-renamed", "parameter path z", "y", "z", "info")]
    # The path renamed, the parameter's declaration not: the rename is still said.
    assert changes(op("/pets/{id}", ("id", "string")), op("/pets/{petId}", ("id", "string"))) == [
        ("path-parameter-renamed", "parameter path petId", "id", "petId", "info")]


def test_compare_a_field_whose_name_or_schema_name_holds_dots(write):
    spec = """
        paths: {}
        components:
          schemas:
            Extra: {type: object, properties: {airline.leg.code: {type: string, maxLength: %d}}}
            Pet.v2: {type: object, properties: {tag: {type: string, maxLength: %d}}}
        """
    root = write({"a/openapi.yaml": HEAD + dedent(spec % (5, 3)), "b/openapi.yaml": HEAD + dedent(spec % (9, 2))})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "a")
    s.load(root / "b/openapi.yaml", "b")
    c = s.compare("Extra.airline.leg.code", "a", "b")  # no schema `Extra.airline.leg`: the field's own name
    assert (c.what, c.a, [(x.kind, x.before, x.after) for x in c.changes]) == (
        "field", "a:field/Extra.airline.leg.code", [("constraint-loosened", "maxLength 5", "maxLength 9")])
    c = s.compare("Pet.v2.tag", "a", "b")  # a schema whose own name has a dot
    assert (c.what, [(x.kind, x.before, x.after) for x in c.changes]) == (
        "field", [("constraint-tightened", "maxLength 3", "maxLength 2")])
