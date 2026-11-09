"""An API split into several root documents, read as one spec: a folder of roots, or a list of them."""

from speclens import load_spec

from conftest import codes


def root(version="3.1.0", info="2.0", paths=""):
    return f"openapi: {version}\ninfo: {{title: t, version: '{info}'}}\npaths:\n{paths}"


PETS = root(paths="""
  /pets:
    get:
      responses:
        '200':
          content:
            application/json:
              schema: {$ref: 'common.yaml#/components/schemas/Pet'}
""")
STORES = root(paths="""
  /stores:
    get:
      responses:
        '200':
          content:
            application/json:
              schema:
                properties:
                  pets: {type: array, items: {$ref: 'common.yaml#/components/schemas/Pet'}}
""")
COMMON = """
components:
  schemas:
    Pet:
      properties:
        name: {type: string}
"""


def test_a_folder_of_roots_is_one_spec(write):
    folder = write({"api/pets.yaml": PETS, "api/stores.yaml": STORES, "api/common.yaml": COMMON,
                    "api/.draft.yaml": root(paths="  /draft: {get: {responses: {'200': {description: ok}}}}\n")})
    s = load_spec(folder / "api")
    assert s.ok, s.issues
    assert s.label == "2.0"  # info.version, the same in every root
    assert [p.name for p in s.roots] == ["pets.yaml", "stores.yaml"]  # common.yaml is a fragment; .draft.yaml is hidden
    assert sorted(o.name for o in s.graph.of_kind("operation")) == ["GET /pets", "GET /stores"]
    pet = s.get_schema("Pet")  # one node, used by operations from both roots
    assert (pet.where, sorted(pet.used_by)) == ("common.yaml:3-5", ["2.0:operation/GET /pets",
                                                                    "2.0:operation/GET /stores"])


def test_a_json_root_in_a_folder(write):
    folder = write({"pets.yaml": PETS, "common.yaml": COMMON,
                    "admin.json": '{"info": {"title": "t", "version": "2.0"}, "openapi": "3.1.0", "paths": '
                                  '{"/admin": {"get": {"responses": {"200": {"description": "ok"}}}}}}',
                    "notes.json": '{"openapi_notes": "not a spec"}'})
    s = load_spec(folder, "v")
    assert [p.name for p in s.roots] == ["admin.json", "pets.yaml"]


def test_a_list_of_roots(write):
    folder = write({"pets.yaml": PETS, "stores.yaml": STORES, "common.yaml": COMMON})
    s = load_spec([folder / "pets.yaml", folder / "stores.yaml"], "v")
    assert s.ok, s.issues
    assert len(s.graph.of_kind("operation")) == 2
    assert load_spec(folder / "pets.yaml", "v").graph.of_kind("operation")[0].name == "GET /pets"  # one root


def test_roots_of_different_openapi_versions(write):
    folder = write({"a.yaml": PETS, "b.yaml": root("3.0.3", paths=STORES.split("paths:\n", 1)[1]),
                    "common.yaml": COMMON})
    s = load_spec(folder, "v")
    assert codes(s) == ["version-mismatch"]
    assert [i.file for i in s.issues] == ["b.yaml"]
    assert [o.name for o in s.graph.of_kind("operation")] == ["GET /pets"]  # the 3.0 file is left out


def test_a_path_in_two_roots(write):
    other = PETS.replace("common.yaml#/components/schemas/Pet", "#/components/schemas/Cat") + \
        "components:\n  schemas:\n    Cat: {type: object}\n"
    folder = write({"a.yaml": PETS, "b.yaml": other, "common.yaml": COMMON})
    s = load_spec(folder, "v")
    assert codes(s) == ["duplicate-path"]
    [issue] = s.issues
    assert (issue.file, issue.line) == ("b.yaml", 5)
    # The first definition is kept, whole.
    assert len(s.graph.of_kind("operation")) == 1
    assert s.get_operation("GET /pets").responses[0].schema == "v:schema/Pet"


def test_the_same_path_item_from_two_roots_is_not_a_conflict(write):
    shared = root(paths="  /pets: {$ref: 'items.yaml#/pets'}\n")
    folder = write({"a.yaml": shared, "b.yaml": shared, "items.yaml": "pets:\n  get:\n    responses:\n"
                                                                       "      '200': {description: ok}\n"})
    s = load_spec(folder, "v")
    assert s.ok, s.issues
    assert [o.name for o in s.graph.of_kind("operation")] == ["GET /pets"]


def test_the_same_component_name_in_two_roots(write):
    a = root(paths="  /a: {get: {responses: {'200': {content: {application/json: {schema: "
                   "{$ref: '#/components/schemas/Error'}}}}}}}\n") + \
        "components:\n  schemas:\n    Error: {properties: {code: {type: integer}}}\n"
    b = root(paths="  /b: {get: {responses: {'200': {content: {application/json: {schema: "
                   "{$ref: '#/components/schemas/Error'}}}}}}}\n") + \
        "components:\n  schemas:\n    Error: {properties: {message: {type: string}}}\n"
    s = load_spec(write({"a.yaml": a, "b.yaml": b}), "v")
    assert s.ok, s.issues
    errors = sorted(n.id for n in s.graph.of_kind("schema") if n.name.endswith("Error"))
    assert len(errors) == 2 and errors[0] != errors[1]  # kept apart, each named by its file
    fields = {e: [f.name for f in s.get_schema(e.split(":", 1)[1].split("/", 1)[1]).fields] for e in errors}
    assert sorted(fields.values()) == [["code"], ["message"]]


def test_labels_and_empty_folders(write):
    folder = write({"x/a.yaml": PETS, "x/b.yaml": STORES.replace("'2.0'", "'2.1'"), "x/common.yaml": COMMON,
                    "empty/readme.txt": "nothing"})
    assert load_spec(folder / "x").label == "x"  # roots disagree on info.version: the folder's name
    assert codes(load_spec(folder / "empty", "v")) == ["no-root"]


def test_an_ignored_root_adds_nothing(write):
    other = root("3.0.3") + "components:\n  schemas:\n    Owner: {properties: {name: {type: string}}}\n"
    s = load_spec(write({"a.yaml": PETS, "b.yaml": other, "common.yaml": COMMON}), "v")
    assert codes(s) == ["version-mismatch"]
    assert [n.name for n in s.graph.of_kind("schema")] == ["Pet"]


def test_the_current_folder_as_a_label(write, monkeypatch):
    folder = write({"x/a.yaml": PETS, "x/b.yaml": STORES.replace("'2.0'", "'2.1'"), "x/common.yaml": COMMON})
    monkeypatch.chdir(folder / "x")
    assert load_spec(".").label == "x"
