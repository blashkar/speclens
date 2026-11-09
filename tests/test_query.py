"""The query API: each function, its text and dict output, and errors that point to real names."""

from pathlib import Path
from textwrap import dedent

import pytest

from speclens import Ambiguous, NotFound, SpecSet, load_spec
from speclens.cli import main

HEAD = "openapi: 3.1.0\ninfo: {title: t, version: '1'}\n"

SHOP = HEAD + dedent("""
    paths:
      /pets:
        get:
          operationId: listPets
          summary: List pets
          tags: [pets]
          parameters:
            - {name: limit, in: query, schema: {type: integer, format: int32}}
          responses:
            '200':
              content:
                application/json:
                  schema: {type: array, items: {$ref: '#/components/schemas/Pet'}}
      /pets/{petId}:
        parameters:
          - {name: petId, in: path, required: true, schema: {type: string}}
        get:
          summary: Find a pet
          responses:
            '200':
              headers:
                X-Rate-Limit: {schema: {type: integer}}
              content:
                application/json:
                  schema: {$ref: '#/components/schemas/Dog'}
            '404':
              content:
                application/json:
                  schema: {$ref: '#/components/schemas/Error'}
        delete:
          deprecated: true
          responses:
            '204': {description: gone}
      /orders:
        post:
          summary: Place an order
          tags: [store]
          requestBody:
            required: true
            content:
              application/json:
                schema: {$ref: '#/components/schemas/Order'}
          responses:
            '201': {description: created}
    components:
      schemas:
        Pet:
          description: A pet in the store.
          required: [name]
          properties:
            name: {type: string, description: The pet's name.}
            status: {type: string, enum: [available, sold], readOnly: true}
            kind: {type: string}
          oneOf:
            - $ref: '#/components/schemas/Cat'
            - $ref: '#/components/schemas/Dog'
          discriminator: {propertyName: kind}
        Cat:
          properties: {indoor: {type: boolean}}
        Dog:
          allOf:
            - $ref: '#/components/schemas/Pet'
            - required: [status]
              properties:
                bark: {type: [string, "null"], description: How it sounds.}
        Order:
          properties:
            pets: {type: array, items: {$ref: '#/components/schemas/Pet'}}
            note: {type: string, deprecated: true}
        Error:
          properties: {message: {type: string}}
        DogBreed: {type: string}
        DogBreeds: {type: array, items: {type: string}}
    """)


@pytest.fixture
def shop(spec):
    s = spec({"openapi.yaml": SHOP})
    assert s.ok and not s.issues, s.issues
    return s


def test_get_schema_lists_own_and_inherited_fields(shop):
    r = shop.get_schema("Dog")
    assert [(f.name, f.type, f.required, f.inherited_from) for f in r.fields] == [
        ("bark", "string|null", False, None),
        ("name", "string", True, "v1:schema/Pet"),
        ("status", "string enum ['available', 'sold']", True, "v1:schema/Pet"),  # required by Dog, not by Pet
        ("kind", "string", False, "v1:schema/Pet"),
    ]
    assert r.used_by == ["v1:operation/GET /pets", "v1:operation/GET /pets/{petId}", "v1:operation/POST /orders"]
    text = r.to_text()
    assert text.splitlines()[0] == "schema Dog [v1:schema/Dog openapi.yaml:63-68]"
    assert "fields (4: 1 own, 3 inherited):" in text
    # A parent's name is said once, above its rows.
    assert "  inherited from Pet (3):\n    name: string, required [v1:field/Pet.name" in text
    assert "    status: string enum ['available', 'sold'], required, readOnly " \
           "[v1:field/Pet.status openapi.yaml:55]" in text
    assert text.count("inherited from Pet") == 1
    assert "bark: string|null [v1:field/Dog.bark openapi.yaml:68] — How it sounds." in text
    assert r.to_dict()["fields"][0]["id"] == "v1:field/Dog.bark"


def test_get_schema_options_with_discriminator_values(shop):
    r = shop.get_schema("pet")  # any case
    assert r.discriminator == "kind"
    assert [(o.type, o.values) for o in r.options] == [("Cat", ["Cat"]), ("Dog", ["Dog"])]
    assert "options (oneOf, discriminator 'kind'):\n  Cat [v1:schema/Cat] when kind = 'Cat'" in r.to_text()
    # The options' own fields apply only under a condition, so they are not Pet's fields.
    assert [f.name for f in r.fields] == ["name", "status", "kind"]
    assert "v1:schema/Cat" in r.conditional_fields


def test_get_schema_by_plain_words_and_errors(shop):
    assert shop.get_schema("orders").id == "v1:schema/Order"
    with pytest.raises(NotFound, match="closest: Order"):
        shop.get_schema("Ordr")
    # "dog breeds" names DogBreeds exactly, and DogBreed as a plural: both are real, so it is ambiguous.
    with pytest.raises(Ambiguous) as e:
        shop.get_schema("dog breeds")
    assert sorted(e.value.candidates) == ["v1:schema/DogBreed", "v1:schema/DogBreeds"]


def test_get_operation(shop):
    r = shop.get_operation("GET", "/pets/{petId}")
    assert r == shop.get_operation("get /pets/{petId}")
    assert [(p.location, p.name, p.type, p.required) for p in r.parameters] == [("path", "petId", "string", True)]
    assert [(b.status, b.media_type, b.header, b.type) for b in r.responses] == [
        ("200", "application/json", None, "Dog"), ("200", None, "X-Rate-Limit", "integer"),
        ("404", "application/json", None, "Error")]
    assert [f.name for f in r.responses[0].fields] == ["bark", "name", "status", "kind"]  # one level deep
    assert r.responses[2].fields == []  # only success responses are expanded
    text = r.to_text()
    assert "  200 application/json: Dog [v1:schema/Dog]" in text
    assert "    inherited from Pet (3):\n      name: string, required [" in text
    listing = shop.get_operation("listPets")
    assert listing.responses[0].type == "array of Pet"
    assert listing.parameters[0].type == "integer (int32)"
    order = shop.get_operation("POST /orders")
    assert [(b.type, b.required) for b in order.request] == [("Order", True)]
    assert "request body (required): application/json: Order [v1:schema/Order]" in order.to_text()
    assert shop.get_operation("DELETE /pets/{petId}").flags == ["deprecated"]


def test_get_operation_is_exact(shop):
    # Two similar paths must never be confused.
    assert shop.get_operation("GET /pets").path == "/pets"
    with pytest.raises(NotFound, match="closest: GET /pets"):
        shop.get_operation("GET /pet")
    with pytest.raises(NotFound):
        shop.get_operation("PUT /pets")


def test_find_field(shop):
    r = shop.find_field("status")
    assert [d.id for d in r.definitions] == ["v1:field/Pet.status"]
    assert [(h.schema, h.inherited, h.required) for h in r.holders] == [
        ("v1:schema/Pet", False, False), ("v1:schema/Dog", True, True)]
    uses = {(u.operation, u.role) for u in r.used_by}
    assert uses == {("v1:operation/GET /pets", "response 200"), ("v1:operation/GET /pets/{petId}", "response 200"),
                    ("v1:operation/POST /orders", "request body")}
    assert "held by 2 schemas:" in r.to_text()
    assert "  Dog (inherited via Pet, required) [v1:schema/Dog]" in r.to_text()
    # A schema and a field follows inheritance to where the field is written.
    assert [d.id for d in shop.find_field("Dog.name").definitions] == ["v1:field/Pet.name"]
    assert [d.id for d in shop.find_field("dog.name").definitions] == ["v1:field/Pet.name"]
    assert [d.id for d in shop.find_field("statuses").definitions] == ["v1:field/Pet.status"]  # plural
    with pytest.raises(NotFound, match="closest: status"):
        shop.find_field("stattus")


def test_list_operations(shop):
    r = shop.list_operations()
    assert [(o.method, o.path) for o in r.operations] == [
        ("POST", "/orders"), ("GET", "/pets"), ("GET", "/pets/{petId}"), ("DELETE", "/pets/{petId}")]
    assert [o.path for o in shop.list_operations("store").operations] == ["/orders"]  # by tag
    assert [o.method for o in shop.list_operations(method="delete").operations] == ["DELETE"]
    assert shop.list_operations(tag="pets").operations[0].id == "v1:operation/GET /pets"
    text = r.to_text()
    assert text.startswith("4 operations in v1:")
    assert "  DELETE /pets/{petId} (deprecated)" in text


def test_search(shop):
    r = shop.search("how it sounds")
    assert r.hits[0].id == "v1:field/Dog.bark"
    assert "field Dog.bark [v1:field/Dog.bark openapi.yaml:68] — How it sounds." in r.to_text()
    assert shop.search("zebra").to_text() == "no matches for 'zebra'"


def test_source(shop):
    r = shop.source_of("v1:field/Pet.name")
    assert r.text.strip() == "name: {type: string, description: The pet's name.}"
    assert shop.source_of("v1:field/Dog.name").id == "v1:field/Pet.name"  # an effective address
    with pytest.raises(NotFound):
        shop.source_of("v1:field/Dog.nothing")


def test_names_in_a_question(shop):
    def found(question):
        return [(m.text, m.kind, m.ids[0], m.how) for m in shop.names_in(question).mentions]

    assert found("What does GET /pets/{petId} return for a Dog?") == [
        ("GET /pets/{petId}", "operation", "v1:operation/GET /pets/{petId}", "path"),
        ("Dog", "schema", "v1:schema/Dog", "identifier")]
    assert found("which orders include dog breeds") == [
        ("orders", "schema", "v1:schema/Order", "plain words"),
        ("dog breeds", "schema", "v1:schema/DogBreed", "plain words")]
    assert found("is listPets paged? what is petId") == [
        ("listPets", "operation", "v1:operation/GET /pets", "identifier"),
        ("petId", "parameter", "v1:parameter//pets/{petId}#path/petId", "identifier")]
    # A common word that is only a field name means nothing on its own...
    assert found("what is the status") == []
    # ...but is that schema's field when the question names the schema.
    assert found("what is the status and name of a pet") == [
        ("status", "field", "v1:field/Pet.status", "field of Pet"),
        ("name", "field", "v1:field/Pet.name", "field of Pet"),
        ("pet", "schema", "v1:schema/Pet", "plain words")]
    assert found("is `status` required") == [("status", "field", "v1:field/Pet.status", "identifier")]
    assert found("nothing here") == []


def test_names_in_follows_the_spec_not_a_word_list(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            subscription:
              properties:
                status: {type: string}
                customer: {$ref: '#/components/schemas/customer'}
            invoice:
              properties:
                subscription: {anyOf: [{type: string}, {$ref: '#/components/schemas/subscription'}]}
                lines: {type: array, items: {$ref: '#/components/schemas/line_item'}}
            customer: {properties: {email: {type: string}}}
            line_item: {properties: {amount: {type: integer}}}
            status: {type: string, enum: [open, paid]}
        """)})

    def found(question):
        return [(m.text, m.kind, m.ids[0]) for m in s.names_in(question).mentions]

    # `subscription` is also a field name, but that field holds a subscription: the word names the thing.
    assert found("cancel a subscription") == [("subscription", "schema", "v1:schema/subscription")]
    # `status` names a schema too, but the `status` field holds a plain string: the word is an attribute.
    assert found("what is the status") == []
    # Another naming style finds the same schema.
    assert found("does a LineItem have an amount") == [("LineItem", "schema", "v1:schema/line_item"),
                                                       ("amount", "field", "v1:field/line_item.amount")]


def test_names_in_finds_fields_in_plain_words(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            Customer:
              properties:
                billingAddress: {$ref: '#/components/schemas/BillingAddress'}
                createdAt: {type: string, format: date-time}
                email: {type: string}
            Invoice:
              properties:
                created_at: {type: string, format: date-time}
                dueDate: {type: string, format: date}
                billingAddressLine: {type: string}
                line2: {type: string}
            BillingAddress: {properties: {city: {type: string}}}
            Coupon: {properties: {code: {type: string}}}
        """)})

    def found(question):
        return [(m.text, m.kind, m.ids, m.how) for m in s.names_in(question).mentions]

    # Every field written that way, in any naming style.
    assert found("when is created at set?") == [
        ("created at", "field", ["v1:field/Customer.createdAt", "v1:field/Invoice.created_at"], "plain words")]
    # Only the mentioned schema's, when it has the field.
    assert found("when is an invoice created at") == [
        ("invoice", "schema", ["v1:schema/Invoice"], "plain words"),
        ("created at", "field", ["v1:field/Invoice.created_at"], "field of Invoice")]
    # All of them when the mentioned schema has none: the question may be wrong about where it is.
    assert found("is the due date on a coupon")[0] == ("due date", "field", ["v1:field/Invoice.dueDate"],
                                                       "plain words")
    # The longest name wins; words naming a schema and a field name both.
    assert found("what is the billing address line")[0][:2] == ("billing address line", "field")
    assert [m[1] for m in found("what is the billing address")] == ["schema", "field"]
    # Plurals; no one-word field from plain words; not across a line break.
    assert found("list the due dates")[0][2] == ["v1:field/Invoice.dueDate"]
    assert found("what is the email") == []
    assert found("is line 2 optional")[0][:3] == ("line 2", "field", ["v1:field/Invoice.line2"])
    assert found("are 2 lines allowed") == []
    assert found("due\ndate") == []


def test_spec_set_queries_by_label(write):
    root = write({"openapi.yaml": SHOP}) / "openapi.yaml"
    specs = SpecSet()
    specs.load(root, "1.2")
    assert specs.get_schema("Dog").id == "1.2:schema/Dog"  # one spec: no label needed
    specs.load(root, "1.10")
    with pytest.raises(KeyError, match="say which spec; loaded: 1.2, 1.10"):
        specs.get_schema("Dog")
    assert specs.get_schema("Dog", "1.10").id == "1.10:schema/Dog"
    assert specs.source("1.2:schema/Cat").id == "1.2:schema/Cat"
    with pytest.raises(KeyError, match="no spec labelled '1.1'"):
        specs.find_field("name", "1.1")


def test_cli_queries(write, capsys):
    root = str(write({"openapi.yaml": SHOP}) / "openapi.yaml")
    assert main(["get-schema", root, "Dog", "--label", "v1"]) == 0
    assert capsys.readouterr().out.startswith("schema Dog [v1:schema/Dog")
    assert main(["get-operation", root, "GET /pets/{petId}", "--json"]) == 0
    assert '"path": "/pets/{petId}"' in capsys.readouterr().out
    assert main(["find-field", root, "bark"]) == 0
    assert main(["list-operations", root, "--filter", "order"]) == 0
    assert main(["search", root, "sounds", "--limit", "1"]) == 0
    assert main(["source", root, "1:schema/Cat"]) == 0
    assert main(["names-in", root, "what does a Dog look like"]) == 0
    capsys.readouterr()
    assert main(["get-schema", root, "Ordr"]) == 1
    assert "no schema named 'Ordr'; closest: Order" in capsys.readouterr().err


LONG = ("Full number. If shown unmasked this is sensitive data, to be sent only over an encrypted channel; "
        "see the security guide for the rules that apply to it and to every other value marked sensitive.")


def test_descriptions_are_whole_in_to_dict_and_cut_only_in_to_text(spec, capsys):
    s = spec({"openapi.yaml": HEAD + dedent(f"""
        paths:
          /cards:
            get:
              description: "{LONG} {LONG}"
              parameters:
                - {{name: q, in: query, description: "{LONG}", schema: {{type: string}}}}
              responses:
                '200': {{description: ok}}
        components:
          schemas:
            Card:
              description: "{LONG} {LONG}"
              properties:
                number: {{type: string, description: "{LONG}"}}
        """)})
    card = s.get_schema("Card")
    assert card.to_dict()["fields"][0]["description"] == LONG
    assert card.to_dict()["description"] == f"{LONG} {LONG}"
    assert "security guide" not in card.to_text().split("number:")[1]  # the row is cut at 100 characters
    assert LONG in card.to_text(full=True).split("number:")[1]
    op = s.get_operation("GET /cards")
    assert op.parameters[0].description == LONG and op.description == f"{LONG} {LONG}"
    assert f"{LONG} {LONG}" in op.to_text(full=True) and f"{LONG} {LONG}" not in op.to_text()
    assert s.search("encrypted channel").hits[0].text == f"{LONG} {LONG}"
    main(["get-schema", str(s.root), "Card", "--full"])
    assert LONG in capsys.readouterr().out


def test_mentions_say_what_they_resolved_to_and_how_they_were_written():
    menu = load_spec(Path(__file__).parent / "fixtures" / "openapi-starter" / "openapi.yaml", "v1")
    found = {(m.text, m.kind): m for m in menu.names_in(
        "Is the `price` of a beverage the same for a menu item in GET /menu?").mentions}
    assert (found[("price", "field")].name, found[("price", "field")].strength) == ("price", "exact")
    assert (found[("beverage", "schema")].name, found[("beverage", "schema")].strength) == ("Beverage", "word")
    assert (found[("menu item", "schema")].name, found[("menu item", "schema")].strength) == ("MenuItem", "phrase")
    assert found[("GET /menu", "operation")].strength == "exact"
    # A capitalised word usually means the schema, but may only start a sentence: a strength of its own.
    [first] = [m for m in menu.names_in("Beverage list").mentions if m.kind == "schema"]
    assert first.strength == "capitalised"


def test_a_parameter_named_in_plain_words_with_or_without_its_x_prefix(write):
    spec = load_spec(write({"openapi.yaml": HEAD + dedent("""
        paths:
          /pets:
            get:
              parameters:
                - {name: X-Request-Id, in: header, schema: {type: string}}
                - {name: page_size, in: query, schema: {type: integer}}
                - {name: x, in: query, schema: {type: integer}}
              responses:
                '200': {description: ok}
        """)}) / "openapi.yaml", "v1")
    found = {(m.text, m.kind, m.name) for m in spec.names_in("Do I send a request id or the page size?").mentions}
    assert found == {("request id", "parameter", "X-Request-Id"), ("page size", "parameter", "page_size")}
    assert not spec.names_in("is x needed?").mentions  # one plain word: not a parameter


def test_words_before_header_or_parameter_name_the_parameter_not_its_type(write):
    spec = load_spec(write({"openapi.yaml": HEAD + dedent("""
        paths:
          /pets:
            get:
              parameters:
                - {name: X-Request-Id, in: header, schema: {$ref: '#/components/schemas/RequestId'}}
                - {name: Vendor-Api-Actor-Type, in: header, schema: {$ref: '#/components/schemas/ActorType'}}
              responses:
                '200': {description: ok}
        components:
          schemas:
            RequestId: {type: string, format: uuid}
            ActorType: {type: string, enum: [user, batch]}
        """)}) / "openapi.yaml", "v1")
    def found(q):
        return {(m.text, m.kind, m.name) for m in spec.names_in(q).mentions}
    assert found("is the request id header required?") == {("request id", "parameter", "X-Request-Id")}
    assert found("what is the actor type header") == {("actor type", "parameter", "Vendor-Api-Actor-Type")}
    assert found("what values does an actor type have?") == {("actor type", "schema", "ActorType")}


def test_operations_written_loosely():
    menu = load_spec(Path(__file__).parent / "fixtures" / "openapi-starter" / "openapi.yaml", "v1")
    def found(q):
        return [(m.text, m.how, m.ids, m.strength) for m in menu.names_in(q).mentions]
    one = ["v1:operation/GET /orders/{orderId}"]
    assert found("what does get /orders/orderId look like") == [("get /orders/orderId", "path (loose)", one, "phrase")]
    assert found("get /orders/42") == [("get /orders/42", "path (loose)", one, "phrase")]
    assert found("the Get orders endpoint") == [("Get orders endpoint", "path (loose)", ["v1:operation/GET /orders"],
                                                 "phrase")]
    assert found("the get order endpoint") == [("get order endpoint", "path (loose)", one, "phrase")]
    assert found("GET /orders/{orderId}") == [("GET /orders/{orderId}", "path", one, "exact")]
    assert found("how to get started with the api") == []
    assert not [m for m in found("get /orders/42/items") if m[1].startswith("path")]  # no such path


def test_expand_gives_the_schemas_an_operation_or_schema_refers_to(capsys):
    menu = load_spec(Path(__file__).parent / "fixtures" / "openapi-starter" / "openapi.yaml", "v1")
    assert menu.get_operation("GET /orders/{orderId}").expanded == []
    op = menu.get_operation("GET /orders/{orderId}", expand=1)
    assert [e.name for e in op.expanded] == ["Order"]  # not the error responses' schema
    assert [e.name for e in menu.get_operation("GET /orders/{orderId}", expand=1, errors=True).expanded] == [
        "Order", "Error"]
    assert all(e.expanded == [] for e in op.expanded)  # one level
    text = op.to_text()
    assert "referenced schemas (1):" in text and "\nschema Order [" in text
    order = menu.get_schema("Order", expand=1)
    assert [e.name for e in order.expanded] == ["OrderStatus"]
    deeper = menu.get_operation("GET /orders/{orderId}", expand=2).expanded
    assert [e.name for e in deeper[0].expanded] == ["OrderStatus"]  # two levels
    assert main(["get-schema", str(Path(__file__).parent / "fixtures" / "openapi-starter" / "openapi.yaml"),
                 "Order", "--expand", "1"]) == 0
    assert "referenced schemas (1):" in capsys.readouterr().out


def test_max_chars_leaves_out_the_deepest_expanded_schemas_first():
    menu = load_spec(Path(__file__).parent / "fixtures" / "openapi-starter" / "openapi.yaml", "v1")
    full = menu.get_operation("GET /orders/{orderId}", expand=2)
    assert [e.name for e in full.expanded[0].expanded] == ["OrderStatus"]
    plain = len(menu.get_operation("GET /orders/{orderId}").to_text())
    one = len(menu.get_operation("GET /orders/{orderId}", expand=1).to_text())
    cut = menu.get_operation("GET /orders/{orderId}", expand=2, max_chars=one + 120)
    assert [e.name for e in cut.expanded] == ["Order"] and cut.expanded[0].expanded == []
    assert cut.expanded[0].omitted == ["v1:schema/OrderStatus"]
    assert "referenced, not shown (over max_chars): OrderStatus" in cut.to_text()
    tiny = menu.get_operation("GET /orders/{orderId}", expand=2, max_chars=10)  # the operation itself stays whole
    assert tiny.expanded == [] and tiny.omitted == ["v1:schema/Order"] and len(tiny.to_text()) > plain
    assert len(menu.get_operation("GET /orders/{orderId}", expand=2, max_chars=10 ** 6).to_text()) == \
        len(full.to_text())


def test_a_method_the_path_does_not_have_gives_the_methods_it_has():
    menu = load_spec(Path(__file__).parent / "fixtures" / "openapi-starter" / "openapi.yaml", "v1")
    def found(q):
        return [(m.how, m.ids) for m in menu.names_in(q).mentions if m.kind == "operation"]
    has = ["v1:operation/GET /revenue"]
    assert found("post /revenue") == [("path (other method)", has)]
    item = found("put /orders/{orderId}")
    assert item == found("put /orders/orderId") and item[0][0] == "path (other method)"
    assert "v1:operation/PATCH /orders/{orderId}" in item[0][1]
    assert found("patch /orders/orderId") == [("path (loose)", ["v1:operation/PATCH /orders/{orderId}"])]


def test_paths_with_the_characters_a_path_segment_may_hold(write):
    spec = HEAD + dedent("""
        paths:
          /pets/$count:
            get: {responses: {'200': {description: ok}}}
          /vets(name={name},city={city}):
            get: {responses: {'200': {description: ok}}}
          /#Action=ListOwners:
            post: {responses: {'200': {description: ok}}}
        """)
    s = load_spec(write({"openapi.yaml": spec}) / "openapi.yaml", "v1")

    def found(text):
        return [(m.text, m.ids, m.how) for m in s.names_in(text).mentions if m.kind == "operation"]

    assert found("How many? GET /pets/$count.") == [("GET /pets/$count", ["v1:operation/GET /pets/$count"], "path")]
    assert found("Look one up (GET /vets(name={name},city={city})).") == [
        ("GET /vets(name={name},city={city})", ["v1:operation/GET /vets(name={name},city={city})"], "path")]
    assert found("Can I PUT /pets/$count?") == [("PUT /pets/$count", ["v1:operation/GET /pets/$count"],
                                                 "path (other method)")]
    assert found("POST /#Action=ListOwners, then stop.")[0][1] == ["v1:operation/POST /#Action=ListOwners"]


def test_names_in_finds_an_operation_by_its_operation_id_in_plain_words(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        paths:
          /shipments:
            post: {operationId: bookShipment, summary: Book a shipment, responses: {'201': {description: ok}}}
          /shipments/{id}:
            get: {operationId: getShipment, responses: {'200': {description: ok}}}
            delete: {operationId: deleteShipment, responses: {'204': {description: gone}}}
          /shipments/{id}/label:
            get: {operationId: getShipmentLabel, responses: {'200': {description: ok}}}
        components:
          schemas:
            Shipment: {properties: {id: {type: string}}}
        """)})

    def ops(question):
        return [(m.text, m.ids) for m in s.names_in(question).mentions if m.kind == "operation"]

    assert ops("How do I book a shipment?") == [("book a shipment", ["v1:operation/POST /shipments"])]
    assert ops("After booking a shipment, what links does it return?") == \
        [("booking a shipment", ["v1:operation/POST /shipments"])]
    assert ops("What happens once the shipment is deleted?") == []  # words in another order spell nothing
    # "a shipment's" belongs to the words after it: the shipment's label, not the shipment.
    assert ops("Is getting a shipment's printed label free?") == \
        [("getting a shipment's printed label", ["v1:operation/GET /shipments/{id}/label"])]
    assert ops("Is getting the shipments' labels free?") == []  # spells neither getShipment nor getShipmentLabel
    assert ops("Does getting a shipment's id need a key?") == []
    assert ops("Once it is booked, a shipment can be tracked.") == []  # not across a comma or full stop
    # A verb form after a determiner, or after another verb the spec's operations start with, describes the
    # thing: deleting a booked shipment is not booking one.
    assert ops("How do I delete a booked shipment?") == []
    assert ops("Can I delete booked shipments?") == []
    assert ops("Once I booked a shipment, can I see it?") == [("booked a shipment", ["v1:operation/POST /shipments"])]
    # Beside the schema the words also name.
    assert "schema" in [m.kind for m in s.names_in("How do I book a shipment?").mentions]


def test_names_in_a_name_both_field_and_parameter_and_a_path_word(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        paths:
          /orders/{orderId}/captures/{captureId}:
            post:
              parameters:
                - {name: orderId, in: path, required: true, schema: {type: string}}
                - {name: captureId, in: path, required: true, schema: {type: string}}
              responses: {'201': {description: ok, content: {application/json: {schema: {$ref: '#/components/schemas/Refund'}}}}}
          /estimateShipping:
            post:
              responses: {'200': {description: ok}}
          /v1/secret:
            get:
              responses: {'200': {description: ok, content: {application/json: {schema: {$ref: '#/components/schemas/Vault'}}}}}
          /petOwners:
            get:
              responses: {'200': {description: ok}}
        components:
          schemas:
            Refund: {properties: {orderId: {type: string}, amount: {type: integer}}}
            Vault: {properties: {secret: {type: [string, 'null']}}}
            PetOwner: {properties: {name: {type: string}}}
        """)})
    assert s.ok, s.issues

    def found(question):
        return [(m.text, m.kind, m.ids, m.how) for m in s.names_in(question).mentions]

    # A field's name that a parameter shares names both.
    assert found("what is orderId") == [
        ("orderId", "field", ["v1:field/Refund.orderId"], "identifier"),
        ("orderId", "parameter", ["v1:parameter/POST /orders/{orderId}/captures/{captureId}#path/orderId"], "identifier")]
    # A name of two words or more that ends a path names the operations there.
    assert found("what does the estimateShipping endpoint return?") == [
        ("estimateShipping", "operation", ["v1:operation/POST /estimateShipping"], "path segment")]
    # Its last fixed segment, with parameters after it or not; one quoted word that names nothing else does too.
    assert found("how do I call `captures`?") == [
        ("captures", "operation", ["v1:operation/POST /orders/{orderId}/captures/{captureId}"], "path segment")]
    # One quoted word that names a field is that field; a version segment is not a name.
    assert found("when `secret` is null") == [("secret", "field", ["v1:field/Vault.secret"], "identifier")]
    assert [m for m in s.names_in("is v1 stable?").mentions if m.kind == "operation"] == []
    # A word naming a schema stays the schema, also in another naming style.
    assert [(m.text, m.kind) for m in s.names_in("how do I call `refunds`?").mentions] == [("refunds", "schema")]
    assert [(m.text, m.kind) for m in s.names_in("list petOwners").mentions] == [("petOwners", "schema")]


def test_rows_show_the_limits_on_a_value(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        paths:
          /pets:
            get:
              parameters:
                - {name: limit, in: query, schema: {type: integer, minimum: 1, maximum: 100}}
                - {name: code, in: query, schema: {$ref: '#/components/schemas/Code'}}
              responses: {'200': {description: ok}}
        components:
          schemas:
            Code: {type: string, minLength: 2, maxLength: 2}
            Short: {allOf: [{$ref: '#/components/schemas/Code'}], pattern: '^[A-Z]+$'}
            Pet:
              properties:
                name: {type: string, maxLength: 40}
                code: {$ref: '#/components/schemas/Code'}
                short: {$ref: '#/components/schemas/Short'}
                tags: {type: array, maxItems: 5, items: {type: string, maxLength: 10}}
                weight: {type: number, exclusiveMinimum: 0}
                age: {type: integer}
        """)})
    rows = {f.name: f.limits for f in s.get_schema("Pet").fields}
    assert rows == {"name": {"maxLength": 40}, "code": {"minLength": 2, "maxLength": 2},
                    "short": {"minLength": 2, "maxLength": 2, "pattern": "^[A-Z]+$"},  # through allOf
                    "tags": {"maxItems": 5},  # the array's own, not its items'
                    "weight": {"exclusiveMinimum": 0}, "age": {}}
    assert "name: string, maxLength 40 [" in s.get_schema("Pet").to_text()
    params = {p.name: p.limits for p in s.get_operation("GET /pets").parameters}
    assert params == {"limit": {"minimum": 1, "maximum": 100}, "code": {"minLength": 2, "maxLength": 2}}
    assert "query limit: integer, minimum 1, maximum 100 [" in s.get_operation("GET /pets").to_text()
    assert "maxLength 2, pattern ^[A-Z]+$ [v1:field/Pet.short" in s.get_schema("Pet").to_text()


def test_a_boolean_exclusive_bound_of_openapi_3_0(spec):
    s = spec({"openapi.yaml": "openapi: 3.0.3\ninfo: {title: t, version: '1'}\npaths: {}\n" + dedent("""
        components:
          schemas:
            Pet:
              properties:
                weight: {type: number, minimum: 0, exclusiveMinimum: true, maximum: 50}
        """)})
    assert s.get_schema("Pet").fields[0].limits == {"exclusiveMinimum": 0, "maximum": 50}


def test_plain_words_spelling_a_path_and_search_by_what_names_a_thing(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        paths:
          /estimateShipping:
            post:
              summary: Estimate the shipping cost
              description: Returns an estimate of what shipping a parcel costs, for a pet owner, by weight and
                destination, before the order is placed. Several carriers are compared and the cheapest is given.
              requestBody:
                content: {application/json: {schema: {$ref: '#/components/schemas/EstimateShippingRequest'}}}
              responses: {'200': {description: ok}}
          /pets:
            get:
              responses: {'200': {description: ok}}
        components:
          schemas:
            EstimateShippingRequest:
              properties:
                express: {type: boolean, description: Whether to ship express.}
                weight: {type: number}
            PetOwner:
              properties:
                name: {type: string}
                address: {type: string}
            Pet: {properties: {owner: {$ref: '#/components/schemas/PetOwner'}}}
        """)})

    def found(question):
        return [(m.text, m.kind, m.ids, m.how) for m in s.names_in(question).mentions]

    # Plain words that spell a path's last segment, also with "the" between, beside what else they name.
    assert found("how do I estimate the shipping?") == [
        ("estimate the shipping", "operation", ["v1:operation/POST /estimateShipping"], "path segment")]
    assert ("estimate shipping", "operation", ["v1:operation/POST /estimateShipping"], "path segment") in \
        found("can I estimate shipping request by weight?")
    assert ("estimate shipping request", "schema", ["v1:schema/EstimateShippingRequest"], "plain words") in \
        found("can I estimate shipping request by weight?")
    # One word is never a path's name in plain words.
    assert [m for m in s.names_in("list the pets").mentions if m.how == "path segment"] == []

    def ranked(query):
        return [h.id for h in s.search(query).hits]

    # The operation's path and summary count against its long description: it ranks above the schema named
    # after it.
    assert ranked("estimate shipping")[0] == "v1:operation/POST /estimateShipping"
    # A field's own name counts more than its schema's: PetOwner's fields come after the schema and after the
    # field named owner.
    r = ranked("pet owner")
    assert r[:2] == ["v1:schema/PetOwner", "v1:field/Pet.owner"]
    assert r.index("v1:field/PetOwner.address") > 1


def test_search_weighs_each_part_of_a_node_apart(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        paths:
          /owners/{ownerId}/pets:
            get:
              summary: List an owner's pets
              description: Returns the pets of one owner, each with its name, tag, breed, age and vaccinations,
                sorted by name, paged by twenty, with the owner's address and phone number on each page.
              parameters:
                - {name: ownerId, in: path, required: true, schema: {type: string}}
              responses: {'200': {description: ok}}
          /petTags:
            get:
              summary: List pet tags
              responses: {'200': {description: ok}}
        components:
          schemas:
            PetTag: {properties: {label: {type: string}}}
            Owner:
              description: An owner, with an owner id, an address and pets.
              properties:
                ownerId: {type: string, description: The id of the owner, unique among owners and stable over time.}
            OwnerPets:
              properties:
                list: {type: array, items: {type: string}}
        """)})

    def ranked(query):
        return [h.id for h in s.search(query).hits]

    # What an operation is called (its summary, its path) counts more than a field that shares one word.
    assert ranked("list owner pets")[0] == "v1:operation/GET /owners/{ownerId}/pets"
    # A whole name equal to the query comes first.
    assert ranked("pet tag")[0] == "v1:schema/PetTag"
    assert ranked("owner")[0] == "v1:schema/Owner"
    assert ranked("owner id")[:2] == ["v1:field/Owner.ownerId", "v1:parameter/GET /owners/{ownerId}/pets#path/ownerId"]
    # A question that names the schema a field belongs to finds that field; one about the schema finds the schema.
    assert ranked("what does label mean on a pet tag?")[0] == "v1:field/PetTag.label"
    assert ranked("what fields does an owner have?")[0] == "v1:schema/Owner"
    # A path written out finds the operation with exactly that path, not a longer one that holds its words.
    assert ranked("what does GET /owners/{ownerId}/pets return?")[0] == "v1:operation/GET /owners/{ownerId}/pets"


def test_search_ranks_a_query_that_reads_like_a_description_by_plain_words(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        paths:
          /stays:
            get:
              summary: Describe a guest's stays
              operationId: describeStays
              responses: {'200': {description: ok}}
          /rooms:
            get:
              summary: List rooms of a hotel
              responses: {'200': {description: ok}}
        components:
          schemas:
            Booking:
              description: Describes a guest's stay at a hotel.
              properties:
                nights: {type: integer}
        """)})

    def ranked(query):
        return [h.id for h in s.search(query).hits]

    # Most of the query's words are in one description: the node it describes comes first, not the operation
    # whose summary shares a few of them.
    assert ranked("describes a guest's stay at a hotel")[0] == "v1:schema/Booking"
    # A short query still weighs what names a thing.
    assert ranked("describe stays")[0] == "v1:operation/GET /stays"


def test_search_puts_the_operation_on_one_item_first_for_get_by_id(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        paths:
          /gadgets/{gadgetId}:
            get: {summary: Get a gadget, responses: {'200': {description: ok}}}
          /widgets/{widgetId}:
            get: {summary: Get a widget, responses: {'200': {description: ok}}}
            put: {summary: Replace a widget, responses: {'200': {description: ok}}}
            delete: {summary: Delete a widget, responses: {'204': {description: gone}}}
          /widgets/{widgetId}/images/{imageId}:
            get: {summary: Get an image of a widget, responses: {'200': {description: ok}}}
          /widgets/{widgetId}/reports:
            get: {summary: List the reports of a widget, responses: {'200': {description: ok}}}
          /widgets/{widgetId}/reports/{reportId}:
            get: {summary: Get a report of a widget by id, responses: {'200': {description: ok}}}
          /widgets/{widgetId}/contact:
            get: {summary: Get the contact of a widget, responses: {'200': {description: ok}}}
          /matters/{matterId}:reopen:
            post: {summary: Reopen a matter by ID, responses: {'200': {description: ok}}}
          /matters/{matterId}:
            get: {summary: Get a matter, responses: {'200': {description: ok}}}
        """)})

    def first(query):
        return s.search(query).hits[0].id

    for query in ("get widget by id", "give me the get widget by Id schema", "fetch a widget by its ID",
                  "get the widget by widgetId", "How do I get a widget with its identifier?"):
        assert first(query) == "v1:operation/GET /widgets/{widgetId}", query
    assert first("delete widget by id") == "v1:operation/DELETE /widgets/{widgetId}"
    assert first("update widget by id") == "v1:operation/PUT /widgets/{widgetId}"
    assert first("get report by id") == "v1:operation/GET /widgets/{widgetId}/reports/{reportId}"
    # A verb it does not know, or an id that is not the item's own, leaves the ranking alone.
    assert first("reopen a matter by ID") == "v1:operation/POST /matters/{matterId}:reopen"
    assert first("get contact by widgetId") == "v1:operation/GET /widgets/{widgetId}/contact"



def test_an_operation_shows_its_callbacks_and_links(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        paths:
          /orders:
            post:
              operationId: placeOrder
              requestBody:
                content: {application/json: {schema: {properties: {notify: {type: string, format: uri}}}}}
              responses:
                '201':
                  description: placed
                  content: {application/json: {schema: {$ref: '#/components/schemas/Order'}}}
                  links:
                    GetOrder: {operationId: getOrder, parameters: {orderId: '$response.body#/id'}}
                    Cancel: {$ref: '#/components/links/Cancel'}
              callbacks:
                orderShipped:
                  '{$request.body#/notify}':
                    post:
                      summary: The order left the warehouse
                      requestBody: {content: {application/json: {schema: {$ref: '#/components/schemas/Shipped'}}}}
                      responses: {'204': {description: received}}
          /orders/{orderId}:
            get:
              operationId: getOrder
              parameters: [{name: orderId, in: path, required: true, schema: {type: string}}]
              responses: {'200': {description: ok}}
        components:
          links:
            Cancel: {operationRef: '#/paths/~1orders~1{orderId}/delete', parameters: {orderId: '$response.body#/id'}}
          schemas:
            Order: {properties: {id: {type: string}}}
            Shipped: {properties: {orderId: {type: string}, at: {type: string, format: date-time}}}
        """)})
    op = s.get_operation("placeOrder")
    assert [(cb["name"], cb["method"], cb["expression"]) for cb in op.callbacks] == \
        [("orderShipped", "POST", "{$request.body#/notify}")]
    assert [b["schema"] for b in op.callbacks[0]["bodies"]] == ["v1:schema/Shipped"]
    assert [(link["status"], link["name"], link.get("operationId"), link.get("operationRef"))
            for link in op.links] == [("201", "GetOrder", "getOrder", None),
                                      ("201", "Cancel", None, "#/paths/~1orders~1{orderId}/delete")]
    text = op.to_text()
    assert "201 GetOrder -> operationId getOrder (orderId = $response.body#/id)" in text
    assert "orderShipped: POST {$request.body#/notify} — The order left the warehouse" in text
    assert "sends application/json: Shipped [v1:schema/Shipped]" in text
    # The schema a callback sends is used by the operation, in that role.
    assert s.get_schema("Shipped").used_by == ["v1:operation/POST /orders"]
    assert [u.role for u in s.find_field("at").used_by] == ["callback orderShipped body"]


def test_rows_name_their_schema_and_keep_every_enum_value(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        paths:
          /pets:
            get:
              parameters: [{name: status, in: query, schema: {$ref: '#/components/schemas/Status'}}]
              responses: {'200': {description: ok}}
        components:
          schemas:
            Status: {type: string, enum: [a, b, c, d, e, f, g, h, i, j]}
            Spot: {properties: {x: {type: number}}}
            Pet:
              properties:
                status: {$ref: '#/components/schemas/Status'}
                spot: {$ref: '#/components/schemas/Spot'}
        """)})
    rows = {f.name: f for f in s.get_schema("Pet").fields}
    # A field whose type is a named schema says which: the schema to look up next.
    assert rows["spot"].type == "Spot"
    assert rows["status"].type.startswith("Status (string enum ['a', 'b',")
    # The default text shows the first values; full text and JSON have them all.
    assert rows["status"].enum == list("abcdefghij")
    assert "'h', …]" in s.get_schema("Pet").to_text()
    assert "'i', 'j']" in s.get_schema("Pet").to_text(full=True)
    assert s.get_schema("Status").to_dict()["enum"] == list("abcdefghij")
    assert "'i', 'j']" in s.get_schema("Status").to_text(full=True)
    [p] = s.get_operation("GET /pets").parameters
    assert p.enum == list("abcdefghij") and "'i', 'j']" in s.get_operation("GET /pets").to_text(full=True)


def test_a_long_named_enum_is_listed_once_not_on_every_row(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        paths:
          /places:
            get:
              parameters: [{name: country, in: query, schema: {$ref: '#/components/schemas/Country'}}]
              responses: {'200': {description: ok}}
        components:
          schemas:
            Country: {type: string, enum: [COUNTRIES]}
            Place:
              properties:
                country: {$ref: '#/components/schemas/Country'}
                kind: {type: string, enum: [KINDS]}
        """).replace("COUNTRIES", ", ".join(f"c{i}" for i in range(30))).replace(
            "KINDS", ", ".join(f"k{i}" for i in range(30)))})
    full = s.get_schema("Place").to_text(full=True)
    # Over 20 values held in a named schema: the row shows the first and says where the rest are.
    assert "country: Country (string enum ['c0', 'c1', 'c2', 'c3', 'c4', 'c5', 'c6', 'c7', … 22 more in Country])" in full
    # Written on the field itself, there is nowhere else to read them: all of them.
    assert "'k28', 'k29']" in full
    assert "'c28', 'c29']" in s.get_schema("Country").to_text(full=True)
    assert s.get_schema("Place").fields[0].enum == [f"c{i}" for i in range(30)]  # JSON has them all
    assert "… 22 more in Country]" in s.get_operation("GET /places").to_text(full=True)


def test_a_parent_schema_shows_its_discriminator_and_who_extends_it(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            Pet:
              required: [kind]
              properties: {kind: {type: string}}
              discriminator: {propertyName: kind, mapping: {cat: '#/components/schemas/Cat', dog: Dog}}
            Cat: {allOf: [{$ref: '#/components/schemas/Pet'}, {properties: {purrs: {type: boolean}}}]}
            Dog: {allOf: [{$ref: '#/components/schemas/Pet'}, {properties: {barks: {type: boolean}}}]}
        """)})
    pet = s.get_schema("Pet")
    assert pet.discriminator_mapping == {"propertyName": "kind",
                                         "mapping": {"cat": "v1:schema/Cat", "dog": "v1:schema/Dog"}}
    assert pet.extended_by == ["v1:schema/Cat", "v1:schema/Dog"]
    text = pet.to_text()
    assert "discriminator 'kind': 'cat' -> Cat, 'dog' -> Dog" in text
    assert "extended by (allOf): Cat, Dog" in text
