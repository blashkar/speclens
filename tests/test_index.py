"""Words from identifiers (text.py) and each index on its own (index.py)."""

from textwrap import dedent

from speclens import text
from speclens.index import Indexes

HEAD = "openapi: 3.1.0\ninfo: {title: t, version: '1'}\n"

STORE = HEAD + dedent("""
    paths:
      /pets/{petId}:
        get:
          operationId: getPet
          summary: Find a pet by its id
          parameters:
            - {name: petId, in: path, required: true, schema: {type: string}}
          responses:
            '200':
              content:
                application/json:
                  schema: {$ref: '#/components/schemas/Pet'}
      /orders:
        post:
          summary: Place an order for pet food
          requestBody:
            content:
              application/json:
                schema:
                  properties:
                    items: {type: array, items: {$ref: '#/components/schemas/LineItem'}}
          responses:
            '201': {description: created}
    components:
      schemas:
        Pet:
          description: A pet in the store.
          properties:
            name: {type: string, description: The pet's name.}
            status: {type: string, enum: [available, sold]}
            owner: {$ref: '#/components/schemas/PetOwner'}
        PetOwner:
          properties:
            name: {type: string}
        LineItem:
          properties:
            sku: {type: string, description: Stock keeping unit of the food.}
        Status: {type: string}
        Category: {type: string}
    """)


def indexes(spec):
    s = spec({"openapi.yaml": STORE})
    return s, Indexes(s.graph)


def test_words_from_identifiers():
    assert text.words("PetOwner") == ["pet", "owner"]
    assert text.words("petId") == ["pet", "id"]
    assert text.words("HTTPError_code") == ["http", "error", "code"]
    assert text.key("pet owner") == text.key("PetOwner") == text.key("pet_owner") == "petowner"
    assert {"category", "status", "pet"} <= text.variants("categories") | text.variants("statuses") | \
        text.variants("pets")
    assert text.terms("Finding the pets") == ["finding", "pet"]


def test_names_exact_case_and_plain_words(spec):
    s, ix = indexes(spec)
    assert ix.names.lookup("schema", "PetOwner") == (["v1:schema/PetOwner"], "exact")
    assert ix.names.lookup("schema", "petowner") == (["v1:schema/PetOwner"], "ignoring case")
    assert ix.names.lookup("schema", "pet owners") == (["v1:schema/PetOwner"], "plain words")
    assert ix.names.lookup("schema", "categories") == (["v1:schema/Category"], "plain words")
    assert ix.names.lookup("operation", "getPet") == (["v1:operation/GET /pets/{petId}"], "exact")
    assert ix.names.lookup("schema", "nothing") == ([], "none")
    # Inline schemas have no name of their own to be found by.
    assert ix.names.lookup("schema", "post orders request") == ([], "none")
    assert "PetOwner" in ix.names.suggest("schema", "PetOwnr")


def test_fields_by_name(spec):
    s, ix = indexes(spec)
    assert sorted(ix.fields.lookup("name")[0]) == ["v1:field/Pet.name", "v1:field/PetOwner.name"]
    assert ix.fields.lookup("SKU") == (["v1:field/LineItem.sku"], "ignoring case")
    assert ix.fields.suggest("statu") == ["status"]


def test_keyword_search_ranks_by_bm25(spec):
    s, ix = indexes(spec)
    top = [i for i, _ in ix.keywords.search("pet food", limit=3)]
    assert top[0] == "v1:operation/POST /orders"  # "pet" and "food" both
    assert ix.keywords.search("stock keeping")[0][0] == "v1:field/LineItem.sku"
    assert [i for i, _ in ix.keywords.search("pet", kinds={"operation"})] == \
        ["v1:operation/GET /pets/{petId}", "v1:operation/POST /orders"]
    assert ix.keywords.search("zebra") == []


def test_used_by_directly_or_nested(spec):
    s, ix = indexes(spec)
    [use] = ix.used_by("v1:schema/Pet")
    assert (use["operation"], use["edge"], use["status"], use["direct"]) == \
        ("v1:operation/GET /pets/{petId}", "returns", "200", True)
    [use] = ix.used_by("v1:schema/PetOwner")
    assert (use["operation"], use["direct"]) == ("v1:operation/GET /pets/{petId}", False)
    [use] = ix.used_by("v1:schema/LineItem")
    assert (use["operation"], use["edge"]) == ("v1:operation/POST /orders", "accepts")
    assert ix.used_by("v1:schema/Status") == []
    [use] = ix.used_by("v1:parameter/GET /pets/{petId}#path/petId")
    assert use["edge"] == "takes"


def test_a_folded_branch_referred_to_elsewhere_is_still_used_by_its_schema(spec):
    s = spec({"openapi.yaml": HEAD + dedent("""
        paths:
          /images:
            post:
              requestBody:
                content:
                  application/json:
                    schema:
                      allOf:
                        - properties: {name: {type: string}}
                        - properties: {tag: {type: string}}
              responses:
                '200': {description: ok}
            put:
              requestBody:
                content:
                  application/json:
                    schema: {$ref: '#/paths/~1images/post/requestBody/content/application~1json/schema/allOf/0'}
              responses:
                '200': {description: ok}
        """)})
    assert s.ok, s.issues
    branch = "v1:schema/POST /images#request/application/json#allOf/0"
    assert sorted(u["operation"] for u in s.index.used_by(branch)) == ["v1:operation/POST /images",
                                                                      "v1:operation/PUT /images"]
    # Still the schema's own fields, not inherited ones.
    fields = {e.target: e.data["inherited"] for e in
              s.graph.outgoing("v1:schema/POST /images#request/application/json", "has_field")}
    assert fields == {"v1:field/POST /images#request/application/json.name": False,
                      "v1:field/POST /images#request/application/json.tag": False}
