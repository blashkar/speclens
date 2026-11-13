"""Checking an answer against its evidence, with answers that are wrong on purpose."""

from pathlib import Path
from textwrap import dedent

import pytest

from speclens import SpecSet, load_spec, verify
from test_compare import HEAD, NEW, OLD

STARTER = Path(__file__).parent / "fixtures" / "openapi-starter" / "openapi.yaml"


def kinds(result):
    return [(f.kind, f.text) for f in result.findings]


@pytest.fixture(scope="module")
def menu():
    return load_spec(STARTER, "v1")


@pytest.fixture
def pets(write):
    root = write({"old/openapi.yaml": OLD, "new/openapi.yaml": NEW})
    s = SpecSet()
    s.load(root / "old/openapi.yaml", "1.2")
    s.load(root / "new/openapi.yaml", "1.10")
    return s


def test_a_correct_answer_has_no_findings(menu):
    evidence = [menu.get_schema("Beverage")]
    answer = ("A beverage has `volume` (a number, required) and `containsCaffeine` "
              "[v1:field/Beverage.containsCaffeine], and inherits `price` from MenuBaseItem "
              "[v1:field/MenuBaseItem.price components/schemas/MenuBaseItem.yaml:29-32].")
    r = verify(answer, evidence)
    assert r.ok, r.to_text()
    assert (r.checked_citations, r.checked_names) == (2, 3)  # MenuBaseItem in prose is not checked as code


def test_a_citation_not_in_the_evidence(menu):
    evidence = [menu.get_schema("Beverage")]
    answer = ("Prices are in cents [v1:field/MenuBaseItem.price], and an order has a total "
              "[v1:field/Order.totalPrice] and a tip [v1:field/Order.tip].")
    # Order.totalPrice exists in the spec but was not in the evidence; Order.tip does not exist at all.
    assert kinds(verify(answer, evidence)) == [("unknown-citation", "v1:field/Order.totalPrice"),
                                               ("unknown-citation", "v1:field/Order.tip")]


def test_an_invented_field(menu):
    evidence = [menu.get_schema("Beverage")]
    r = verify("Each beverage has a `sugarLevel` and a servingTemperature, besides its `volume`.", evidence)
    assert kinds(r) == [("unknown-name", "sugarLevel"), ("unknown-name", "servingTemperature")]
    assert "nowhere in the evidence" in r.to_text()


def test_names_from_json_and_http_are_not_findings(menu):
    evidence = [menu.get_operation("GET /orders/{orderId}")]
    answer = "Send `GET /orders/{orderId}`; the response is `application/json` with `id` as a string, `null` never."
    assert verify(answer, evidence).ok


def test_a_false_missing_claim_is_contradicted(pets):
    # Both versions' Pet were in the evidence, yet the answer says 1.2 lacks name.
    evidence = [pets.get_schema("Pet", "1.2"), pets.get_schema("Pet", "1.10")]
    r = verify("In 1.2 the Pet schema is missing `name`, which 1.10 has.", evidence)
    [f] = r.findings
    assert (f.kind, f.evidence) == ("contradicted-claim", ["1.2"])
    assert f.detail == "the evidence shows name in Pet in 1.2"


def test_a_missing_claim_backed_by_a_comparison(pets):
    evidence = [pets.compare("Pet", "1.2", "1.10")]
    assert verify("`nickname` was removed in 1.10.", evidence).ok
    assert verify("`age` is missing in 1.2, it was added in 1.10.", evidence).ok
    # The comparison shows owner.phone removed, not owner.
    assert kinds(verify("`owner` was removed in 1.10.", evidence)) == [
        ("unsupported-claim", "`owner` was removed in 1.10.")]
    assert verify("`phone` was removed in 1.10.", evidence).ok
    # The comparison shows Owner.email in 1.10 (its format changed): it was not removed.
    assert kinds(verify("`email` was removed in 1.10.", evidence)) == [
        ("contradicted-claim", "`email` was removed in 1.10.")]


def test_a_missing_claim_backed_by_a_full_field_list(pets):
    evidence = [pets.get_schema("Pet", "1.10")]
    assert verify("In 1.10, Pet has no `nickname`.", evidence).ok
    # A field list of Pet says nothing about Owner.
    assert {f.kind for f in verify("In 1.10, Owner has no `phone`.", evidence).findings} == {
        "unsupported-claim", "unknown-name"}


def test_a_claim_with_no_evidence_for_the_version(pets):
    evidence = [pets.get_schema("Pet", "1.10")]
    r = verify("The `status` field was added in 1.10.", evidence + [("1.2", "status: {type: string}")])
    [f] = r.findings
    assert (f.kind, f.evidence) == ("contradicted-claim", ["1.2"])  # a 1.2 chunk shows it already there
    r = verify("The `age` field was added in 1.10.", evidence)
    assert kinds(r) == [("unsupported-claim", "The `age` field was added in 1.10.")]


def test_raw_chunks_with_labels(pets):
    # A search-based bot: chunks of YAML, each from a known version.
    chunks = [("1.2", "Pet:\n  properties:\n    id: {type: integer}\n    nickname: {type: string}"),
              ("1.10", "Pet:\n  properties:\n    id: {type: number}\n    age: {type: integer}")]
    assert kinds(verify("1.10 does not have `nickname`, unlike 1.2.", chunks)) == [
        ("unsupported-claim", "1.10 does not have `nickname`")]  # a chunk is not the whole schema
    assert kinds(verify("`age` is not in 1.10.", chunks)) == [("contradicted-claim", "`age` is not in 1.10.")]
    assert kinds(verify("Pets have a `colour`.", chunks)) == [("unknown-name", "colour")]


def test_labels_match_exactly(pets):
    evidence = [pets.get_schema("Pet", "1.2")]
    # "11.2" and "1.20" are not "1.2": no claim about 1.2 here, so nothing to check.
    assert verify("`name` is missing in 11.2 and in 1.20.", evidence).checked_claims == 0


def test_a_change_claim_needs_a_comparison(pets):
    evidence = [pets.get_schema("Pet", "1.2"), pets.get_schema("Pet", "1.10")]
    assert kinds(verify("The type of `id` changed in 1.10.", evidence)) == [
        ("unsupported-claim", "The type of `id` changed in 1.10.")]
    assert verify("The type of `id` changed in 1.10.", evidence + [pets.compare("Pet", "1.2", "1.10")]).ok


def test_evidence_as_dicts_or_text(menu):
    result = menu.get_schema("Beverage")
    answer = "It has `volume` [v1:field/Beverage.volume]."
    assert verify(answer, [result.to_dict()]).ok
    r = verify(answer, [result.to_text()])
    assert r.ok  # ids are read from the text too
    assert {f.kind for f in verify(answer, ["nothing relevant"]).findings} == {"unknown-citation", "unknown-name"}


def test_output(menu):
    r = verify("A `sugarLevel` field [v1:field/Beverage.sugarLevel].", [menu.get_schema("Beverage")])
    text = r.to_text()
    assert text.startswith("checked 1 citations, 1 names, 0 version claims, 0 counts: 2 findings")
    assert "unknown-citation: 'v1:field/Beverage.sugarLevel' — this id is not in the evidence" in text
    assert r.to_dict()["ok"] is False


def test_an_id_with_brackets_in_its_name(write):
    spec = load_spec(write({"openapi.yaml": "openapi: 3.1.0\ninfo: {title: t, version: '1'}\ncomponents:\n"
                                            "  schemas:\n    A:\n      properties:\n"
                                            "        'option[n]': {type: string}\n"}) / "openapi.yaml", "v1")
    evidence = [spec.get_schema("A")]
    assert verify("A has an option [v1:field/A.option[n] openapi.yaml:7].", evidence).ok
    assert verify("A has an option [v1:field/A.option[n]].", evidence).ok
    assert kinds(verify("See [v1:field/A.option[m]].", evidence)) == [("unknown-citation", "v1:field/A.option[m]")]


def test_the_schema_named_in_a_claim_is_where_to_look(pets):
    evidence = [pets.get_schema("Pet", "1.2"), pets.get_schema("Owner", "1.2"), pets.get_schema("Pet", "1.10")]
    # age is new in Pet; Pet itself existed before, which does not contradict the claim.
    assert verify("`age` was added to Pet in 1.10.", evidence).ok
    # Owner in 1.2 has email but not name: the claim is about Owner, not about any `name` in 1.2.
    assert verify("In 1.2, Owner has no `name`.", evidence).ok
    assert kinds(verify("In 1.2, Owner has no `email`.", evidence)) == [
        ("contradicted-claim", "Owner has no `email`.")]  # the clause the claim is in


def test_hyphenated_names(write):
    spec = load_spec(write({"openapi.yaml": "openapi: 3.1.0\ninfo: {title: t, version: '1'}\ncomponents:\n"
                                            "  schemas:\n    rate-limit:\n      properties:\n"
                                            "        reset_at: {type: string}\n"}) / "openapi.yaml", "v1")
    evidence = [spec.get_schema("rate-limit")]
    assert verify("The rate-limit schema has `reset_at` [v1:field/rate-limit.reset_at].", evidence).ok
    assert verify("In v1, rate-limit has no `remaining`.", evidence).ok  # its full field list says so


def test_a_pronoun_refers_to_what_was_just_named(pets):
    chunks = [("1.2", "Pet:\n  properties:\n    nickname: {type: string}"),
              ("1.10", "Pet:\n  properties:\n    age: {type: integer}")]
    r = verify("In 1.10 Pet includes `age`, but 1.2 lacks it.", chunks)
    assert kinds(r) == [("unsupported-claim", "1.2 lacks it.")]  # a chunk is not Pet's whole field list
    r = verify("In 1.2 Pet has `nickname`, but 1.10 lacks it.", chunks + [pets.get_schema("Pet", "1.10")])
    assert r.ok  # it is nickname, in Pet: 1.10's full field list of Pet has no nickname
    r = verify("1.2 has `nickname`, but 1.10 lacks it.", chunks + [pets.get_schema("Pet", "1.10")])
    assert kinds(r) == [("unsupported-claim", "1.10 lacks it.")]  # which schema? a field list cannot say
    r = verify("1.10 has `age`, but it is not in 1.10.", chunks)
    assert kinds(r) == [("contradicted-claim", "it is not in 1.10.")]


def test_ordinary_technical_words_are_not_findings(menu):
    evidence = [menu.get_operation("GET /orders/{orderId}")]
    answer = ("Call it from JavaScript, TypeScript or a Node.js backend, from iOS or macOS apps, or from GitHub "
              "Actions; it is described in the OpenAPI document at https://api.example.com/v1/openapi.yaml?api_key=x. "
              "Send `Authorization: Bearer <token>` and `Accept: application/json`; mail support@example.com. "
              "It returns 1 order, removed from nothing.")
    r = verify(answer, evidence)
    assert r.ok, r.to_text()
    assert r.checked_claims == 0


def test_invented_names_written_as_code_are_still_caught(menu):
    evidence = [menu.get_operation("GET /orders/{orderId}")]
    r = verify("It returns an `OrderReceipt` with a `tipAmount` and a delivery_window.", evidence)
    assert kinds(r) == [("unknown-name", "OrderReceipt"), ("unknown-name", "tipAmount"),
                        ("unknown-name", "delivery_window")]


def test_a_bare_number_label_needs_a_cue(write):
    root = write({"a/openapi.yaml": OLD, "b/openapi.yaml": NEW})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "1")
    s.load(root / "b/openapi.yaml", "2")
    evidence = [s.get_schema("Pet", "1"), s.get_schema("Pet", "2")]
    assert verify("It returns 1 pet; `name` is required.", evidence).checked_claims == 0
    r = verify("In version 1, Pet has no `name`.", evidence)
    assert kinds(r) == [("contradicted-claim", "Pet has no `name`.")]
    assert kinds(verify("Pet has no `name` in v1.", evidence)) == [("contradicted-claim", "Pet has no `name` in v1.")]


def test_names_with_a_sign_or_a_capital_first_letter(write):
    spec = load_spec(write({"openapi.yaml": "openapi: 3.1.0\ninfo: {title: t, version: '1'}\ncomponents:\n"
                                            "  schemas:\n    Doc:\n      properties:\n"
                                            "        $id: {type: string}\n        '@type': {type: string}\n"
                                            "        sendSMS: {type: boolean}\n"}) / "openapi.yaml", "v1")
    evidence = [spec.get_schema("Doc")]
    assert verify("A Doc has `$id`, `@type` and `sendSMS`; SendSMS is optional.", evidence).ok
    assert kinds(verify("A Doc has `$uid`.", evidence)) == [("unknown-name", "$uid")]


def test_a_claim_naming_both_versions_is_about_the_change(pets):
    evidence = [pets.compare("Pet", "1.2", "1.10")]
    assert verify("`nickname` was removed between 1.2 and 1.10.", evidence).ok
    assert verify("`age` was added from 1.2 to 1.10.", evidence).ok
    assert kinds(verify("`email` was removed between 1.2 and 1.10.", evidence)) == [
        ("contradicted-claim", "`email` was removed between 1.2 and 1.10.")]


def test_a_v_before_a_dotted_label(pets):
    evidence = [pets.compare("Pet", "1.2", "1.10")]
    assert verify("`nickname` was removed in v1.10.", evidence).ok
    assert kinds(verify("`email` was removed in v1.10.", evidence)) == [
        ("contradicted-claim", "`email` was removed in v1.10.")]


def test_a_diff_backs_a_claim(pets):
    evidence = [pets.diff("1.2", "1.10")]
    assert verify("`nickname` was removed in 1.10.", evidence).ok
    assert {f.kind for f in verify("`colour` was removed in 1.10.", evidence).findings} == {
        "unsupported-claim", "unknown-name"}


def test_evidence_in_a_list_is_read_as_its_items(pets):
    comparison = pets.compare("Pet", "1.2", "1.10")
    assert verify("`nickname` was removed in 1.10.", [[comparison]]).ok
    assert verify("`nickname` was removed in 1.10.", [(comparison, pets.get_schema("Pet", "1.10"))]).ok


def test_a_field_list_with_options_is_not_complete(write):
    spec = load_spec(write({"openapi.yaml": "openapi: 3.1.0\ninfo: {title: t, version: '1'}\ncomponents:\n"
                                            "  schemas:\n    Pet:\n      properties: {id: {type: string}}\n"
                                            "      oneOf:\n        - properties: {bark: {type: boolean}}\n"
                                            "        - properties: {meow: {type: boolean}}\n"}) / "openapi.yaml",
                     "1.2")
    evidence = [spec.get_schema("Pet")]
    # bark is under an option: the field list does not show it is absent.
    assert ("unsupported-claim", "Pet has no `bark`.") in kinds(verify("In 1.2, Pet has no `bark`.", evidence))
    assert kinds(verify("In 1.2, Pet has no `id`.", evidence)) == [("contradicted-claim", "Pet has no `id`.")]


def test_the_new_name_after_replaced_by_is_not_what_was_removed(write):
    old = ("openapi: 3.1.0\ninfo: {title: t, version: '1.0'}\ncomponents:\n  schemas:\n"
           "    Foo: {properties: {a: {type: string}, b: {type: string}, c: {type: string}}}\n")
    root = write({"a/openapi.yaml": old, "b/openapi.yaml": old.replace("Foo", "Bar").replace("'1.0'", "'2.0'")})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "1.0")
    s.load(root / "b/openapi.yaml", "2.0")
    evidence = [s.compare("Foo", "1.0", "2.0"), s.get_schema("Bar", "2.0")]
    for answer in ("In 2.0, the `Foo` schema was removed and replaced by `Bar`.",
                   "In 2.0, `Foo` was renamed to `Bar`.", "`Foo` was dropped in 2.0 in favour of `Bar`."):
        assert verify(answer, evidence).ok, answer
    assert kinds(verify("In 2.0, `Bar` was removed and replaced by `Foo`.", evidence)) == [
        ("contradicted-claim", "`Bar` was removed and replaced by `Foo`.")]


def test_prose_before_the_word_field_is_not_a_name(pets):
    evidence = [pets.get_schema("Pet", "1.10")]
    r = verify("The 1.10 files include Pet, though they do not define security policies beyond what is in the "
               "schema and field descriptions.", evidence)
    assert r.ok and r.checked_claims == 0
    # A real field, or a name written like code, still is one.
    assert kinds(verify("In 1.10 there is no name field.", evidence)) == [
        ("contradicted-claim", "In 1.10 there is no name field.")]


def test_counts_of_fields(menu):
    evidence = [menu.get_schema("Beverage")]  # 12 fields: 3 own, 9 inherited
    for answer in ("Beverage has 12 fields.", "Beverage has 3 fields of its own and inherits 9 fields from "
                   "MenuBaseItem.", "Beverage has twelve properties, 9 inherited.",
                   "It has 12 fields in total.", "Beverage has 3 own fields."):
        r = verify(answer, evidence)
        assert r.ok and r.checked_counts >= 1, (answer, r.to_text())
    for answer in ("Beverage has 14 fields.", "Beverage inherits 11 fields from MenuBaseItem.",
                   "Beverage has 12 fields, 4 inherited.", "Beverage has 5 own fields."):
        [f] = verify(answer, evidence).findings
        assert f.kind == "wrong-count", answer
    [f] = verify("Beverage inherits 11 fields from MenuBaseItem.", evidence).findings
    assert f.detail.endswith("9 from MenuBaseItem")
    [f] = verify("Beverage has 14 fields.", evidence).findings
    assert f.detail.startswith("14 fields stated; the evidence shows Beverage in v1: 12 total, 3 own, 9 inherited")


def test_counts_of_parameters(menu):
    evidence = [menu.get_operation("GET /orders/{orderId}")]  # one path parameter, required
    assert verify("`GET /orders/{orderId}` takes 1 parameter, a path parameter.", evidence).ok
    assert verify("getOrderById has one required parameter.", evidence).ok
    [f] = verify("`GET /orders/{orderId}` takes 2 query parameters.", evidence).findings
    assert f.kind == "wrong-count"


def test_numbers_that_are_not_counts_are_left_alone(menu):
    evidence = [menu.get_schema("Beverage"), menu.get_operation("GET /orders/{orderId}")]
    for answer in ("It returns 200 with the order; at most 50 fields are shown per page.",
                   "Beverage has at least 3 fields.", "The first 2 fields of Beverage are required.",
                   "Its volume is 500 and price is 3 dollars.", "Beverage's `price` has 2 fields.",
                   "3 fields were added to Beverage in v1.", "Some menu item has 30 fields.",
                   "Beverage has 4 fields more than its parent.", "One of the fields is `volume`."):
        r = verify(answer, evidence)
        assert not [f for f in r.findings if f.kind == "wrong-count"], (answer, r.to_text())


def test_no_count_check_when_the_list_is_not_complete(write):
    spec = load_spec(write({"openapi.yaml": "openapi: 3.1.0\ninfo: {title: t, version: '1'}\ncomponents:\n"
                                            "  schemas:\n    Pet:\n      properties: {id: {type: string}}\n"
                                            "      oneOf:\n        - properties: {bark: {type: boolean}}\n"
                                            "        - properties: {meow: {type: boolean}}\n"}) / "openapi.yaml",
                     "1")
    evidence = [spec.get_schema("Pet")]
    r = verify("Pet has 2 fields and 2 options.", evidence)  # fields under options: not counted
    assert r.ok and r.checked_counts == 1
    assert [f.kind for f in verify("Pet has 3 options.", evidence).findings] == ["wrong-count"]
    # Text chunks hold no complete list.
    assert verify("Pet has 9 fields.", [("1", "Pet:\n  properties:\n    id: {type: string}")]).checked_counts == 0


def test_a_count_for_each_version(pets):
    evidence = [pets.get_schema("Pet", "1.2"), pets.get_schema("Pet", "1.10")]  # 2 required, then 1
    assert verify("In 1.2, Pet has 2 required fields.", evidence).ok
    assert [f.kind for f in verify("In 1.10, Pet has 2 required fields.", evidence).findings] == ["wrong-count"]
    assert verify("Pet has 1 required field.", evidence).ok  # no version named: either one may be meant


def test_several_ids_in_one_bracket(menu):
    evidence = [menu.get_schema("Beverage"), menu.get_schema("Dessert"), menu.get_operation("GET /menu")]
    for answer in ("Fields [v1:schema/Beverage, v1:schema/Dessert].",
                   "Op [v1:operation/GET /menu, v1:schema/Beverage].", "Op [v1:operation/GET /menu;v1:schema/Dessert]."):
        r = verify(answer, evidence)
        assert r.ok and r.checked_citations == 2, (answer, r.to_text())
    assert kinds(verify("Op [v1:operation/GET /menu; v1:schema/Nope].", evidence)) == [
        ("unknown-citation", "v1:schema/Nope")]
    # A comma inside a name is still part of it when no id follows.
    from speclens.verify import _cited_ids
    assert [i for i, _ in _cited_ids("[v1:field/Pet.a,b]")] == ["v1:field/Pet.a,b"]


def test_an_operation_is_not_named_inside_a_longer_path(write):
    head = "openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths:\n"
    param = "        - {name: %s, in: query, schema: {type: string}}\n"
    spec = load_spec(write({"openapi.yaml": head + "  /pets:\n    get:\n      parameters:\n" + param % "limit"
                            + "  /pets/{petId}:\n    get:\n      parameters:\n"
                            + "        - {name: petId, in: path, required: true, schema: {type: string}}\n"
                            + param % "fields"}) / "openapi.yaml", "1")
    evidence = [spec.get_operation("GET /pets"), spec.get_operation("GET /pets/{petId}")]
    assert [f.kind for f in verify("GET /pets/{petId} has 1 parameter.", evidence).findings] == ["wrong-count"]
    assert verify("GET /pets/{petId} has 2 parameters and GET /pets has 1.", evidence).ok


def test_a_qualifier_after_the_count(pets):
    evidence = [pets.get_schema("Pet", "1.2"), pets.get_schema("Pet", "1.10")]  # 2 required, then 1
    assert verify("In 1.10 only one field is required in Pet.", evidence).ok
    assert [f.kind for f in verify("In 1.2 only one field is required in Pet.", evidence).findings] == [
        "wrong-count"]
    assert not [f for f in verify("In Pet, `name` is one field you must send.", evidence).findings
                if f.kind == "wrong-count"]


def test_a_field_named_in_plain_words_in_a_claim(write):
    def version(v, extra):
        return (f"openapi: 3.1.0\ninfo: {{title: t, version: '{v}'}}\ncomponents:\n  schemas:\n    Pet:\n"
                f"      properties:\n        name: {{type: string}}\n{extra}")
    root = write({"a/openapi.yaml": version("1.0", ""),
                  "b/openapi.yaml": version("2.0", "        birthDate: {type: string}\n")})
    s = SpecSet()
    s.load(root / "a/openapi.yaml")
    s.load(root / "b/openapi.yaml")
    evidence = [s.compare("Pet", "1.0", "2.0")]
    assert verify("The birth date field was added in 2.0.", evidence).ok
    r = verify("The color field was removed in 2.0.", evidence)
    assert [f.kind for f in r.findings] == ["unsupported-claim"], r.to_text()


def test_a_negation_about_something_a_name_has_is_not_a_claim_that_it_is_absent(pets):
    evidence = [pets.get_schema("Pet", "1.10")]
    for answer in ("There is no encryption applied to `name` in 1.10.", "`name` has no default in 1.10.",
                   "Therefore, there is no indication that hashing is applied to the `name` in 1.10.",
                   "There is no validation on `name` in 1.10.", "Encryption is no longer applied to `name` in 1.10."):
        r = verify(answer, evidence)
        assert r.ok and r.checked_claims == 0, (answer, r.to_text())
    for answer in ("There is no `name` in 1.10.", "`name` is missing in 1.10.", "In 1.10, Pet does not have `name`.",
                   "In 1.10 there is no optional `name` field.", "Pet lacks the `name` field in 1.10.",
                   "In 1.10 Pet no longer has `name`."):
        assert [f.kind for f in verify(answer, evidence).findings] == ["contradicted-claim"], answer


def test_known_names_are_not_unknown_but_back_nothing(pets):
    evidence = [pets.get_schema("Pet", "1.10")]
    earlier = "Earlier: `Vet` holds `clinicPhone`."
    assert kinds(verify("As said, `Vet` has `clinicPhone`.", evidence)) == [("unknown-name", "Vet"),
                                                                           ("unknown-name", "clinicPhone")]
    assert verify("As said, `Vet` has `clinicPhone`.", evidence, known=[earlier]).ok
    assert verify("`clinicPhone`", evidence, known=["clinicPhone"]).ok  # names, too
    # Known is not evidence: a version claim still needs the evidence.
    assert kinds(verify("`clinicPhone` was added in 1.10.", evidence, known=[earlier])) == [
        ("unsupported-claim", "`clinicPhone` was added in 1.10.")]


def test_cover_reports_breaking_changes_the_answer_leaves_out(pets):
    c = pets.compare("POST /pets", "1.2", "1.10")
    breaking = [ch for ch in c.changes if ch.level == "breaking"]
    assert len(breaking) == 4
    full = ("POST /pets now requires a body, `name` is limited to 20 characters, `kind` is required and no longer "
            "accepts 'bird'.")
    r = verify(full, [], cover=[c])
    assert r.ok and r.checked_changes == 4, r.to_text()
    r = verify("POST /pets now requires a body and `kind`, which no longer accepts 'bird'.", [], cover=[c])
    assert [f.kind for f in r.findings] == ["omitted-change"] and "name (maxLength 50 -> maxLength 20)" in \
        r.findings[0].text
    assert verify(full, [c]).checked_changes == 0  # only with cover
    assert r.findings[0].places == ["POST /pets"] and r.to_dict()["findings"][0]["places"] == ["POST /pets"]


def test_cover_a_whole_diff(pets):
    d = pets.diff("1.2", "1.10")
    r = verify("GET /stores was removed.", [], cover=[d])
    assert "GET /stores" not in " ".join(f.detail for f in r.findings)
    assert all(f.kind == "omitted-change" for f in r.findings) and len(r.findings) == r.checked_changes - 1
    for f in r.findings:  # every place it was reached from, as data: the first is named in the detail
        assert f.places and f" in {f.places[0]}" in f.detail
        assert (f"(and {len(f.places) - 1} more)" in f.detail) == (len(f.places) > 1)
    assert not verify("`tag` is missing in 1.2.", [pets.get_schema("Pet", "1.2")]).findings[0].places


def test_cover_places_of_a_change_reached_from_several_operations(write):
    def spec(kind):
        ops = "".join(f"""
              /{p}:
                get:
                  responses:
                    '200':
                      content:
                        application/json:
                          schema: {{$ref: '#/components/schemas/Vet'}}""" for p in ("vets", "clinics", "owners"))
        return HEAD + dedent(f"""
            paths:{ops}
            components:
              schemas:
                Vet: {{properties: {{license: {{type: {kind}}}}}}}
            """)
    root = write({"a/openapi.yaml": spec("string"), "b/openapi.yaml": spec("integer")})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "1.0")
    s.load(root / "b/openapi.yaml", "2.0")
    r = verify("Nothing changed.", [], cover=[s.diff("1.0", "2.0")])
    assert len(r.findings) == 1, r.to_text()
    assert sorted(r.findings[0].places) == ["GET /clinics", "GET /owners", "GET /vets"]
    assert "(and 2 more)" in r.findings[0].detail


def test_function_words_are_never_what_a_claim_is_about():
    # Chunks of prose, as a RAG pipeline passes them: every English word is in them.
    evidence = [("1.2", "Pets are listed in the store, and such pets have a `tag`.\ntag: string"),
                ("1.10", "Pets are listed in the store and have a `region`.\nregion: string")]
    assert verify("These fields are not present in 1.2 and are added in 1.10 for faster lookups.", evidence).ok
    assert verify("1.2 has no such endpoint.", evidence).ok
    # A name the evidence has as a key is still checked as a plain word.
    assert kinds(verify("There is no tag in 1.2.", evidence)) == [("contradicted-claim", "There is no tag in 1.2.")]


def test_a_word_that_describes_the_next_one_is_not_said_to_be_absent():
    evidence = [("1.2", "encryption: none\nPets are stored as given."), ("1.10", "encryption: aes256")]
    assert verify("1.2 does not include explicit encryption guidance.", evidence).ok
    assert verify("1.2 lacks encryption fields of its own.", evidence).ok
    assert kinds(verify("1.2 has no `encryption` field.", evidence)) == [
        ("contradicted-claim", "1.2 has no `encryption` field.")]
    assert kinds(verify("`encryption` is missing in 1.2.", evidence)) == [
        ("contradicted-claim", "`encryption` is missing in 1.2.")]


def test_required_or_optional_claims_against_the_evidence(pets):
    schemas = [pets.get_schema("Pet", "1.2"), pets.get_schema("Pet", "1.10")]
    assert verify("`name` is required in 1.2.", schemas).ok
    r = verify("`name` is required in 1.10.", schemas)
    assert kinds(r) == [("contradicted-claim", "`name` is required in 1.10.")] and r.checked_required == 1
    assert r.findings[0].detail == "the evidence shows name optional in 1.10"
    assert verify("`name` is required.", schemas).ok  # true in one version, not the other: not checked
    assert verify("`Pet.id` is required, and `name` is optional in 1.10.", schemas).ok
    assert kinds(verify("`Pet.id` is optional.", schemas)) == [("contradicted-claim", "`Pet.id` is optional.")]
    op = [pets.get_operation("GET /pets", label="1.10")]
    assert kinds(verify("`limit` is a required parameter.", op)) == [
        ("contradicted-claim", "`limit` is a required parameter.")]
    assert verify("`region` is required; `limit` is not required.", op).ok
    assert verify("`limit` is required when paging, and the `required` keyword is set on `region`.", op).ok
    # A comparison is evidence too: region was added as a required parameter.
    c = [pets.compare("GET /pets", "1.2", "1.10")]
    assert kinds(verify("1.10 adds an optional parameter `region`.", c)) == [
        ("contradicted-claim", "1.10 adds an optional parameter `region`.")]
    assert verify("Required parameter added: `region`.", c).ok


def test_cover_counts_values_unwrapping_and_headers_in_words(write):
    def spec(types, item, header):
        return HEAD + dedent(f"""
            paths:
              /files:
                get:
                  parameters:
                    - {{name: {header}, in: header, required: true, schema: {{type: string}}}}
                  responses:
                    '200':
                      content:
                        application/json:
                          schema:
                            properties:
                              type: {{type: string, enum: [{types}]}}
                              item: {item}
            components:
              schemas:
                Cat: {{properties: {{purr: {{type: boolean}}}}}}
                Dog: {{properties: {{bark: {{type: boolean}}}}}}
            """)
    wrapped = "{oneOf: [{properties: {cat: {$ref: '#/components/schemas/Cat'}}}, " \
              "{properties: {dog: {$ref: '#/components/schemas/Dog'}}}]}"
    plain = "{oneOf: [{$ref: '#/components/schemas/Cat'}, {$ref: '#/components/schemas/Dog'}]}"
    root = write({"a/openapi.yaml": spec("text/plain", wrapped, "X-Trace"),
                  "b/openapi.yaml": spec("text/plain, application/zip", plain, "X-Trace-Request-Id")})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "1")
    s.load(root / "b/openapi.yaml", "2")
    c = s.compare("GET /files", "1", "2")
    assert {ch.kind for ch in c.changes if ch.level == "breaking"} == {
        "enum-value-added", "options-unwrapped", "required-parameter-added"}
    full = ("GET /files may now return application/zip, the item is no longer wrapped in an object, and it needs "
            "a trace request id header.")
    r = verify(full, [c], cover=[c])
    assert r.ok and r.checked_changes == 3, r.to_text()
    r = verify("GET /files now returns a Cat or a Dog directly and may return application/zip; send "
               "`X-Trace-Request-Id`.", [c], cover=[c])
    assert r.ok, r.to_text()
    r = verify("GET /files has new types.", [c], cover=[c])
    assert len(r.findings) == 3 and {f.kind for f in r.findings} == {"omitted-change"}


def test_required_said_of_a_path_is_said_of_its_last_name(write):
    def spec(required):
        return HEAD + dedent(f"""
            paths:
              /owners/{{id}}:
                get:
                  parameters:
                    - {{name: id, in: path, required: true, schema: {{type: string}}}}
                  responses:
                    '200':
                      content:
                        application/json:
                          schema: {{$ref: '#/components/schemas/Owner'}}
            components:
              schemas:
                Owner:
                  properties:
                    pets: {{type: array, items: {{$ref: '#/components/schemas/Pet'}}}}
                Pet:
                  required: [{required}]
                  properties:
                    name: {{type: string}}
                    tag: {{type: string}}
            """)
    root = write({"a/openapi.yaml": spec("name"), "b/openapi.yaml": spec("name, tag")})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "1.0")
    s.load(root / "b/openapi.yaml", "2.0")
    evidence = [s.compare("GET /owners/{id}", "1.0", "2.0"), s.get_schema("Owner", "1.0"), s.get_schema("Owner", "2.0")]
    assert verify("New required `pets[].tag`.", evidence).ok  # pets itself is optional; tag became required
    assert verify("`pets[].tag` is now required.", evidence).ok
    r = verify("In 2.0 there is an optional `pets[].tag`.", evidence)
    assert kinds(r) == [("contradicted-claim", "In 2.0 there is an optional `pets[].tag`.")]
    assert r.findings[0].detail == "the evidence shows tag required in 2.0"
    assert kinds(verify("A required `pets` list.", evidence)) == [("contradicted-claim", "A required `pets` list.")]
    # No version named, but "new" or "now" says the latest: tag was optional in 1.0, is required in 2.0.
    r = verify("New optional `pets[].tag`.", evidence)
    assert kinds(r) == [("contradicted-claim", "New optional `pets[].tag`.")]
    assert r.findings[0].detail == "the evidence shows tag required in 2.0"
    assert verify("`pets[].tag` is optional.", evidence).ok  # true in 1.0, not in 2.0: not checked


def test_these_and_it_refer_only_to_names_the_evidence_has():
    evidence = [("1.2", "tag: string"), ("1.10", "other: text")]
    # A reference in brackets is where a statement comes from, not what "these fields" are; one the evidence
    # does not have is an unknown name, and nothing more.
    answer = "See [_chunk_pets_0]. These fields are not present in 1.10 and are added in 1.2."
    assert [f.kind for f in verify(answer, evidence).findings] == ["unknown-name"]
    assert verify(answer, [("1.2", "_chunk_pets_0: tag: string"), ("1.10", "other: text")]).ok
    # An invented name is not what a later "it" is, either.
    r = verify("Pets have a `tagLine`. It is not present in 1.10.", evidence)
    assert [f.kind for f in r.findings] == ["unknown-name"]
    r = verify("1.2 has `tag`. It is not present in 1.10.", evidence)
    assert [f.kind for f in r.findings] == ["unsupported-claim"] and "tag" in r.findings[0].detail


def test_one_unsupported_claim_is_one_finding():
    evidence = [("1.2", "OldPet: name: string"), ("1.10", "NewPet: name: string")]
    r = verify("`OldPet` (1.2) changed to `NewPet` (1.10), renamed and reclassified.", evidence)
    assert [f.kind for f in r.findings] == ["unsupported-claim"]
    assert r.findings[0].detail == "no comparison in the evidence shows OldPet, NewPet changed in 1.2, 1.10"


def test_expanded_schemas_are_evidence_of_their_own(menu):
    op = menu.get_operation("GET /orders/{orderId}", expand=1)
    assert [e.name for e in op.expanded] == ["Order"]
    order = next(e for e in op.expanded if e.name == "Order")
    answer = f"The Order schema has {len(order.fields)} fields."
    assert verify(answer, [op]).checked_counts == 1 and verify(answer, [op]).ok
    assert not verify(f"The Order schema has {len(order.fields) + 1} fields.", [op]).ok


def test_claims_about_a_path_and_the_version_where_it_still_was(write):
    def spec(owner_pet):
        return HEAD + dedent(f"""
            paths:
              /owners:
                get:
                  responses:
                    '200':
                      content:
                        application/json:
                          schema: {{$ref: '#/components/schemas/Owner'}}
            components:
              schemas:
                Owner:
                  properties:
                    pets: {{type: array, items: {{properties: {owner_pet}}}}}
            """)
    root = write({"a/openapi.yaml": spec("{tag: {type: array, items: {type: string}}, name: {type: string}}"),
                  "b/openapi.yaml": spec("{tags: {type: array, items: {type: string}}, name: {type: integer}}")})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "1.0")
    s.load(root / "b/openapi.yaml", "2.0")
    c = [s.compare("GET /owners", "1.0", "2.0")]
    assert {(ch.kind, ch.where.split(": ")[-1]) for ch in c[0].changes} >= {("field-removed", "pets[].tag"),
                                                                            ("field-added", "pets[].tags")}
    # The version a removal names as where it still was stands for the one after it.
    for answer in ("| `pets[].tag` | Removed (was array of strings in 1.0) |",
                   "`pets[].tag` was removed in 2.0 (it was an array of strings in 1.0).",
                   "`pets[].tag` used to be a list in 1.0 and is gone now.",
                   "`pets[].tags` was added in 2.0.",
                   "`pets[].name` changed in 2.0."):
        assert verify(answer, c).ok, (answer, verify(answer, c).to_text())
    assert kinds(verify("`pets[].tag` was removed in 1.0.", c)) == [
        ("contradicted-claim", "`pets[].tag` was removed in 1.0.")]
    assert kinds(verify("`pets[].name` was added in 2.0.", c)) == [
        ("contradicted-claim", "`pets[].name` was added in 2.0.")]
    r = verify("`pets[].color` was removed in 2.0.", c)
    assert [f.kind for f in r.findings] == ["unknown-name", "unsupported-claim"]


def test_required_status_said_of_being_required_is_not_a_field(pets):
    schemas = [pets.get_schema("Pet", "1.2"), pets.get_schema("Pet", "1.10")]  # status: optional in both
    def contradicted(answer):
        return [f for f in verify(answer, schemas).findings if f.kind == "contradicted-claim"]

    assert not contradicted("In 1.10 only descriptions and required status changed for Pet.")
    assert not contradicted("The required status of `status` did not change.")
    assert not contradicted("Optional flags were updated in 1.10.")
    # Written as code, or followed by "field", it is the field.
    assert not verify("`status` is required in 1.10.", schemas).ok
    assert not verify("A required status field.", schemas).ok
    assert not verify("A required `status`.", schemas).ok


def test_an_operation_said_not_to_exist_is_checked_by_method_and_path(pets):
    op = [pets.get_operation("DELETE /pets/{id}", label="1.10")]
    r = verify("DELETE /pets/{id} does not exist in 1.10.", op)
    assert kinds(r) == [("contradicted-claim", "DELETE /pets/{id} does not exist in 1.10.")]
    assert r.findings[0].detail == "the evidence shows DELETE /pets/{id} in 1.10"
    assert "contradicted-claim" in [f.kind for f in verify("DELETE /pets/{petId} is not available in 1.10.", op).findings]
    # Another method on the same path says nothing against it; the methods the path has back it.
    r = verify("PUT /pets/{id} does not exist in 1.10; only DELETE does.", op)
    assert [f.kind for f in r.findings] == ["unsupported-claim"]
    asked = [pets.names_in("Can I PUT /pets/{id}?", "1.10")]
    assert verify("PUT /pets/{id} does not exist in 1.10; only DELETE does.", op + asked).ok
    # Named beside something else, the operation is where it is: about tag, not GET /pets.
    assert verify("GET /pets has no `tag` parameter in 1.10.", [pets.compare("GET /pets", "1.2", "1.10")]).ok


def test_a_kind_before_a_name_is_not_a_name(pets):
    assert verify("Pet changed in 1.10.", [pets.compare("Pet", "1.2", "1.10")]).ok  # the comparison of Pet itself
    c = [pets.compare("GET /pets", "1.2", "1.10")]
    assert verify("Operation: GET /pets changed in 1.10.", c).ok
    assert verify("In 1.10, GET /pets changed (operation: GET /pets).", c).ok


def test_operations_with_any_path_characters_and_labels_with_words(write):
    def spec(methods):
        return HEAD + "paths:\n" + "".join(
            f"  {p}:\n    {m}: {{responses: {{'200': {{description: ok}}}}}}\n" for p, m in methods)
    root = write({"a/openapi.yaml": spec([("/pets/$count", "get"), ("/vets(name={name})", "get"),
                                          ("/vets(name={name},city={city})", "get")]),
                  "b/openapi.yaml": spec([("/pets/$count", "get"), ("/vets(name={name})", "delete")])})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "1.0-beta")
    s.load(root / "b/openapi.yaml", "2.0-streaming")
    op = [s.get_operation("GET /pets/$count", label="2.0-streaming")]
    r = verify("GET /pets/$count does not exist in 2.0-streaming.", op)
    assert [f.kind for f in r.findings] == ["contradicted-claim"], r.to_text()
    d = [s.diff("1.0-beta", "2.0-streaming")]
    assert verify("GET /vets(name={name}) was removed in 2.0-streaming.", d).ok
    one = [s.get_operation("GET /vets(name={name},city={city})", label="1.0-beta")]
    r = verify("GET /vets(name={name},city={city}) does not exist in 1.0-beta.", one)  # one path, not two clauses
    assert [f.detail for f in r.findings] == ["the evidence shows GET /vets(name={name},city={city}) in 1.0-beta"]
    r = verify("GET /pets/$count was removed in 2.0-streaming.", d + op)
    assert [f.kind for f in r.findings] == ["contradicted-claim"], r.to_text()


def test_cover_names_starting_with_the_place_values_as_written_and_what_gained_them(write):
    def spec(options, category, kind, addresses):
        refs = ", ".join("{$ref: '#/components/schemas/%s'}" % o for o in options)
        return HEAD + "paths: {}\ncomponents:\n  schemas:\n" + "".join(f"    {line}\n" for line in [
            f"Pet: {{oneOf: [{refs}]}}",
            "Cat: {properties: {purr: {type: boolean}}}",
            "PetCat: {properties: {meow: {type: boolean}}}",
            f"Payment: {{properties: {{category: {{type: string, enum: [{category}]}}, "
            f"kind: {{type: string, enum: [{kind}]}}}}}}",
            f"Owner: {{properties: {{name: {{type: string}}{addresses}}}}}"])
    inline = ", addresses: {type: object, properties: {city: {type: string}}}"
    root = write({"a/openapi.yaml": spec(["Cat"], "CARD", "CARD", inline),
                  "b/openapi.yaml": spec(["Cat", "PetCat"], "CARD, GIFT_CARD", "CARD, GIFTCARD, VOUCHER", "")})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "1.0")
    s.load(root / "b/openapi.yaml", "2.0")
    # The place's name is left out only as a whole name: PetCat is still named.
    c = s.compare("Pet", "1.0", "2.0")
    assert verify("Pet gained a new option, `PetCat`.", [c], cover=[c]).ok
    assert [f.kind for f in verify("Pet gained a new option.", [c], cover=[c]).findings] == ["omitted-change"]
    # Two values with the same plain key are two values: naming one is not naming the other.
    p = s.compare("Payment", "1.0", "2.0")
    r = verify("In 2.0 `kind` gained `GIFTCARD`.", [p], cover=[p])  # kind holds GIFTCARD, which is new
    # Values of kind are named, so VOUCHER must be too; GIFT_CARD is another value, of category.
    assert [f.text for f in r.findings] == [
        "breaking: enum value added: category (none -> 'GIFT_CARD') [2.0:field/Payment.category]",
        "breaking: enum value added: kind (none -> 'VOUCHER') [2.0:field/Payment.kind]"], r.to_text()
    assert verify("In 2.0 `kind` gained `GIFTCARD` and `VOUCHER`, and `category` gained `GIFT_CARD`.", [p],
                  cover=[p]).ok
    assert verify("In 2.0 `kind` and `category` gained new values.", [p], cover=[p]).ok  # the fields, no values
    # A whole thing removed, named by its own name.
    for name in ("Owner", "Owner.addresses"):  # the field in its owner, or the inline schema itself
        o = s.compare(name, "1.0", "2.0")
        r = verify("`Owner.addresses` was removed in 2.0.", [o], cover=[o])
        assert r.ok, (name, r.to_text())


def test_limits_claimed_against_the_limits_shown(write):
    def spec(name_max):
        return HEAD + dedent(f"""
            paths:
              /pets:
                get:
                  parameters:
                    - {{name: limit, in: query, schema: {{type: integer, minimum: 1, maximum: 100}}}}
                  responses: {{'200': {{description: ok}}}}
            components:
              schemas:
                Pet:
                  properties:
                    name: {{type: string, maxLength: {name_max}}}
                    tags: {{type: array, maxItems: 5, items: {{type: string}}}}
                    nickname: {{type: string, description: 'At most 20 characters.'}}
                Owner:
                  properties:
                    name: {{type: string, minLength: 1}}
            """)
    root = write({"a/openapi.yaml": spec(80), "b/openapi.yaml": spec(40)})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "1.0")
    s.load(root / "b/openapi.yaml", "2.0")
    pet = s.get_schema("Pet", "2.0")
    for true in ("`name` is at most 40 characters.", "`Pet.name`: maximum 40 characters.",
                 "The maximum length of `name` is 40.", "`name` has a maxLength of 40.", "`tags` holds up to 5 items.",
                 "`name` can't exceed 40 characters, and `tags` 5 items.", "`name` must be between 0 and 40 characters."):
        r = verify(true, [pet])
        assert r.ok and r.checked_limits, true
    r = verify("`name`: maximum 100 characters.", [pet])
    assert kinds(r) == [("contradicted-claim", "`name`: maximum 100 characters.")]
    assert r.findings[0].detail == "the evidence shows name: maxLength 40 in 2.0"
    assert not verify("`tags` holds up to 9 items.", [pet]).ok
    assert not verify("`name`: must be plain text, at most 100 characters.", [pet]).ok  # the name before the colon
    # A limit the evidence does not show, said roughly, or only sometimes, is not checked.
    for unchecked in ("`nickname` is at most 20 characters.", "`name` is about 100 characters.",
                      "`name` is at most 100 characters when the pet is a dog."):
        r = verify(unchecked, [pet])
        assert r.ok and not r.checked_limits, unchecked
    # Saying there is no limit: wrong when the evidence shows one, fine when it shows none.
    r = verify("`name` has no maximum length.", [pet])
    assert kinds(r) == [("contradicted-claim", "`name` has no maximum length.")]
    assert r.findings[0].detail == "the evidence shows name: maxLength 40 in 2.0"
    assert not verify("`tags` has no defined minimum or maximum constraints.", [pet]).ok
    assert verify("`nickname` has no maximum length.", [pet]).ok
    # More ways to say a limit: each checked, and caught with another number.
    for said, wrong in (("The maximum length of `name` in `Pet` is 40 characters.", "64"),
                        ("`name` has a minimum length of 0 and a maximum length of 40.", "64"),
                        ("`name` is a string with a `maxLength` of 40.", "64"),
                        ("The maximum `name` in `Pet` is 40.", "64"),
                        ("A `Pet` can have at most five `tags`.", "six"),
                        ("`tags` has a `maxItems 5` constraint.", "6"),
                        ("`name`:\n- Maximum: `40` characters", "64")):
        assert verify(said, [pet]).checked_limits, said
        assert verify(said, [pet]).ok, said
        bad = said.replace("five", wrong) if "five" in said else said.replace("40", wrong).replace("5", wrong)
        assert not verify(bad, [pet]).ok, bad
    # JSON Schema's own keywords are not unknown names, even when the evidence does not show them.
    assert verify("`nickname` has no `maxLength`, `minimum` or `maxItems`.", [pet]).ok
    # The version said, and a schema named beside the field.
    both = [s.get_schema("Pet", "1.0"), pet, s.get_schema("Owner", "2.0")]
    assert verify("In 1.0, `name` is at most 80 characters.", both).ok
    assert not verify("In 2.0, `name` is at most 80 characters.", both).ok
    assert verify("`name` is at most 80 characters.", both).ok  # true in 1.0
    assert verify("`Owner.name` is at least 1 character.", both).ok
    assert not verify("`Owner.name` is at least 3 characters.", both).ok
    # Parameters, and what a comparison shows before and after.
    op = s.get_operation("GET /pets", label="2.0")
    assert verify("`limit` is at most 100.", [op]).ok
    assert not verify("`limit` must be at least 0.", [op]).ok
    c = s.compare("Pet", "1.0", "2.0")
    assert verify("In 1.0 `name` was at most 80 characters, in 2.0 at most 40.", [c]).ok
    assert not verify("In 2.0, `name` is at most 80 characters.", [c]).ok


def test_limits_said_with_words_between_or_of_a_name_said_before(write):
    root = write({"openapi.yaml": HEAD + dedent("""
        paths:
          /owners:
            get:
              parameters:
                - {name: X-Trace-Id, in: header, schema: {type: string, minLength: 8, maxLength: 64}}
              responses: {'200': {description: ok}}
        components:
          schemas:
            Owner:
              properties:
                nickname: {type: string, minLength: 1, maxLength: 40}
                age: {type: integer, format: int32, minimum: 0, maximum: 120}
                tags: {type: array, minItems: 1, items: {type: string}}
                note: {type: string}
                ownerKind: {type: string}
        """)})
    s = SpecSet()
    s.load(root / "openapi.yaml", "2.0")
    ev = [s.get_schema("Owner", "2.0"), s.get_operation("GET /owners", label="2.0")]
    # Each is checked, and caught with the last number changed.
    for said, number, wrong in (
            ("The maximum length of `nickname` in `Owner` in 2.0 is 40 characters.", "40", "64"),
            ("`age` in `Owner` (2.0) is an integer with a minimum value of 0.", "0", "18"),
            ("The `age` field in the `Owner` schema allows values with a minimum of 0.", "0", "18"),
            ("`age` is an integer (`int32`) with a minimum value of 0 and a maximum value of 120.", "120", "150"),
            ("The minimum age in `Owner` is 0.", "0", "18"),  # one field of Owner spells it
            ("The maximum nickname length for `Owner` is **40**.", "40", "64"),
            ("There is a `minItems 1` constraint on the `tags` array.", "1", "2"),
            ("The `tags` field is defined with `minItems 1`.", "1", "2"),
            ("The `X-Trace-Id` header in the `GET /owners` operation has a maximum length of 64.", "64", "99"),
            ("The `X-Trace-Id` header in the `GET /owners` endpoint in 2.0 has a maximum length of 64.", "64", "99"),
            ("`nickname` is a display name. It is defined as a string with a `maxLength` of 40.", "40", "64"),
            ("`nickname` holds a display name. The field has a `minLength` of 1.", "1", "2"),
            ("- **Field:** `nickname`\n- **Limits:** minimum 1, maximum 40", "40", "64"),
            ("`age` holds a number of years. It must be between 0 and 120, inclusive.", "120", "150")):
        r = verify(said, ev)
        assert r.ok and r.checked_limits, said
        assert not verify(wrong.join(said.rsplit(number, 1)), ev).ok, said
    # Saying there is no limit, with the name after.
    assert not verify("There is no maximum length for `nickname` in `Owner`.", ev).ok
    assert not verify("No minimum or maximum length is defined for `nickname`.", ev).ok
    assert verify("There is no maximum length for `note` in `Owner`.", ev).ok
    # "It" is the one name with limits just before, and only that.
    assert verify("`note` is free text. It has a maximum of 5.", ev).ok
    # `ownerK*` names a family: known when the evidence has a name it begins.
    assert verify("Names like `ownerK*` hold kinds.", ev).ok
    assert [f.kind for f in verify("Names like `zebra*` hold kinds.", ev).findings] == ["unknown-name"]


def test_a_limit_said_of_the_field_named_before_or_by_the_sentence(write):
    root = write({"openapi.yaml": HEAD + dedent("""
        paths:
          /owners:
            get:
              responses: {'200': {description: ok}}
          /owners/{id}:
            get:
              parameters: [{name: id, in: path, required: true, schema: {type: string}}]
              responses: {'200': {description: ok}}
        components:
          schemas:
            Owner:
              properties:
                nickname: {type: string, minLength: 1, maxLength: 40}
                tags: {type: array, minItems: 1, items: {type: string}}
        """)})
    s = SpecSet()
    s.load(root / "openapi.yaml", "2.0")
    ev = [s.get_schema("Owner", "2.0"), s.get_operation("GET /owners", label="2.0")]
    for said, number, wrong in (
            # "the field" in a later sentence is the one field named just before
            (("The `tags` field lists labels. Although the field is optional, the `minItems 1` rule means at least "
              "one label."), "minItems 1", "minItems 8"),
            ("`nickname` is a name. Although the field is optional, it has a maximum length of 40.", "40", "64"),
            # a sentence naming one field and some operations is still about the field
            (("The `tags` field is in the responses of `GET /owners` and `GET /owners/{id}`, and the `minItems 1` "
              "rule applies to it."), "1", "8"),
            (("The `tags` field is in the responses of `GET /owners` and `GET /owners/{id}` and has a `minItems` "
              "of 1."), "1", "8"),
            # "if it is present" only says the value is there, which every limit assumes
            (("The `tags` field lists labels. Although the field is optional, the `minItems 1` constraint means that "
              "if the field is present, it holds a label."), "minItems 1", "minItems 8"),
            ("`nickname` is a name. Although the field is optional, `minLength 1` applies if it is present.",
             "minLength 1", "minLength 8"),
            ("When provided, `tags` must have at least 1 item.", "1", "8"),
            ("If present, `nickname` has a maximum length of 40.", "40", "64")):
        r = verify(said, ev)
        assert r.ok and r.checked_limits, said
        assert not verify(wrong.join(said.rsplit(number, 1)), ev).ok, said
    # A limit of something else in such a sentence is not the field's.
    for said in ("`nickname` is optional. Although the field is optional, the list holds at most 50 owners.",
                 "The `nickname` field is in `Owner`, and `GET /owners` returns at most 50 owners.",
                 "The `nickname` field is in `Owner`, and a page holds at most 50 owners.",
                 "The `nickname` field is returned by `GET /owners`, which returns at most 50 owners.",
                 # a real condition still makes the limit conditional
                 "`nickname` has a maximum length of 64 when used for search.",
                 "`nickname` has a maximum length of 64 if the owner is a company.",
                 "When used for search, the `nickname` field has a maximum length of 64.",
                 "`nickname` has a maximum length of 64, if the owner is a company."):
        assert verify(said, ev).ok, said


def test_limits_said_under_a_version_or_after_a_lead_in(write):
    owner = """
        components:
          schemas:
            Owner:
              properties:
                nickname: {type: string%s}
                age: {type: integer, minimum: 0, maximum: 120}
                tags: {type: array, minItems: 1, items: {type: string}}
        """
    root = write({"v1/openapi.yaml": HEAD + dedent(owner % ""),
                  "v2/openapi.yaml": HEAD + dedent(owner % ", minLength: 1, maxLength: 40")})
    s = SpecSet()
    s.load(root / "v1/openapi.yaml", "1.0")
    s.load(root / "v2/openapi.yaml", "2.0")
    ev = [s.get_schema("Owner", "2.0")]
    # Each is checked, and caught with the last number changed.
    for said, number, wrong in (
            ("The maximum length of `nickname` in the `Owner` schema in Acme 2.0 is 40 characters.", "40", "64"),
            ("The `nickname` field of an `Owner` in Acme 2.0 can be up to 40 characters long.", "40", "64"),
            ("The minimum age in `Owner` in 2.0 is 0.", "0", "18"),
            ("The `age` field allows values from 0 to 120.", "120", "150"),
            (("The `nickname` field has the following length constraints:\n- **Minimum length:** 1\n"
              "- **Maximum length:** 40"), "40", "64"),
            ("Under the `tags` field:\n- **Limits:** `minItems 1`", "1", "2"),
            ("The `age` field has a minimum of 0. This means the value must be between 0 and 120, inclusive.",
             "120", "150")):
        r = verify(said, ev)
        assert r.ok and r.checked_limits, said
        assert not verify(wrong.join(said.rsplit(number, 1)), ev).ok, said
    # Under a heading naming a version, a claim is about that version only.
    both = [s.get_schema("Owner", "1.0"), s.get_schema("Owner", "2.0")]
    compared = ("#### **Acme 1.0**\n- **Field**: `nickname`\n- **Constraints**: No minimum or maximum specified\n"
                "#### **Acme 2.0**\n- **Field**: `nickname`\n- **Constraints**:\n  - Minimum: `1`\n  - Maximum: `40`")
    assert verify(compared, both).ok
    assert not verify(compared.replace("`40`", "`64`"), both).ok
    assert not verify(compared.replace("Acme 1.0", "Acme 0").replace("Acme 2.0", "Acme 1.0"), both).ok


def test_required_said_under_a_version_heading_or_in_a_record(write):
    owner = """
        components:
          schemas:
            Owner:%s
              properties:
                nickname: {type: string}
                age: {type: integer, minimum: 0, maximum: 120}
        """
    root = write({"v1/openapi.yaml": HEAD + dedent(owner % ""),
                  "v2/openapi.yaml": HEAD + dedent(owner % "\n              required: [nickname]")})
    s = SpecSet()
    s.load(root / "v1/openapi.yaml", "1.0")
    s.load(root / "v2/openapi.yaml", "2.0")
    both = [s.get_schema("Owner", "1.0"), s.get_schema("Owner", "2.0")]
    for right, wrong in (
            ("#### **Acme 1.0**\n- `nickname` is optional.\n#### **Acme 2.0**\n- `nickname` is required.",
             "#### **Acme 1.0**\n- `nickname` is required.\n#### **Acme 2.0**\n- `nickname` is optional."),
            (("#### Acme 1.0\n- **Field**: `nickname`\n- **Required**: No\n"
              "#### Acme 2.0\n- **Field**: `nickname`\n- **Required**: Yes"),
             ("#### Acme 1.0\n- **Field**: `nickname`\n- **Required**: Yes\n"
              "#### Acme 2.0\n- **Field**: `nickname`\n- **Required**: No"))):
        assert verify(right, both).ok, right
        assert [f.kind for f in verify(wrong, both).findings] == ["contradicted-claim"] * 2, wrong
    # "This means the value ..." after a list about one name is about that name.
    ev = [s.get_schema("Owner", "2.0")]
    listed = "- **Field:** `age`\n- **Limits:** minimum 0, maximum 120\nThis means the value must be between 0 and {}."
    assert verify(listed.format(120), ev).ok
    assert not verify(listed.format(150), ev).ok
    assert verify("- **Type:** string\nThis means the value must be at most 5.", ev).ok


def test_a_field_named_like_a_word_is_not_read_as_that_word(write):
    root = write({"openapi.yaml": HEAD + dedent("""
        components:
          schemas:
            Entry:
              required: [message]
              properties:
                message: {type: string}
                in: {type: string}
                added: {type: string}
                since: {type: string, format: date}
                deprecated: {type: boolean}
        """)})
    s = SpecSet()
    s.load(root / "openapi.yaml", "2.0")
    ev = [s.get_schema("Entry", "2.0")]
    # "in" after "required" is a word, not the optional field `in`.
    assert verify("The `message` field is required in `Entry`.", ev).ok
    assert verify("In 2.0, `message` is required in `Entry`.", ev).ok
    # `added`, `since` and `deprecated` as code are names, not claims that something was added or changed.
    for name in ("added", "since", "deprecated"):
        r = verify(f"In 2.0, `{name}` is optional in `Entry`.", ev)
        assert r.ok, (name, r.findings)
    # The words themselves still make claims.
    assert not verify("In 2.0, `message` is optional in `Entry`.", ev).ok
    assert not verify("`nickname` was added to `Entry` in 2.0.", ev).ok



def test_something_added_inside_a_field_is_not_the_field_added(write):
    pet = """
        components:
          schemas:
            Status: {type: string, enum: [new, sold%s]}
            Pet:
              properties:
                name: {type: string}
                age: {type: integer%s}
                status: {$ref: '#/components/schemas/Status'}
                wasStatus: {$ref: '#/components/schemas/Status'}
        """
    root = write({"a/openapi.yaml": HEAD + dedent(pet % ("", "")),
                  "b/openapi.yaml": HEAD.replace("'1'", "'2'") + dedent(pet % (", lost", ", minimum: 0, maximum: 30"))})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "1.0")
    s.load(root / "b/openapi.yaml", "2.0")
    for ev in ([s.compare("Pet", "1.0", "2.0"), s.get_schema("Pet", "1.0"), s.get_schema("Pet", "2.0")],
               [s.compare("Pet.age", "1.0", "2.0"), s.compare("Pet", "1.0", "2.0")]):
        for said in ("In 2.0, `lost` was added to the `status` enum in `Pet`.",
                     "- **Enum value added**: `'lost'` was added to the allowed values of `status` in 2.0.",
                     "In 2.0, `age` in `Pet` now has a `minimum` constraint of 0.",
                     "The field `age` in `Pet` now has both a minimum (0) and a maximum (30) in 2.0.",
                     "In 2.0, a minimum of 0 was added to `age`.",
                     "In 2.0, `status` added the enum value `lost`.",
                     "In 2.0, `age` has added a maximum of 30."):
            r = verify(said, ev)
            assert r.ok, (said, r.to_text())
        # The field itself was there before.
        assert [f.kind for f in verify("In 2.0, `age` was added to `Pet`.", ev).findings] == ["contradicted-claim"]
    # A change to a schema two fields share is listed once, at `status`: it backs a claim about `wasStatus` too.
    rows = [s.compare("Pet", "1.0", "2.0"), s.get_schema("Pet", "1.0"), s.get_schema("Pet", "2.0")]
    assert verify("In 2.0, `lost` was added to the `wasStatus` enum.", rows).ok
    # Made up: the comparison covers `name`, and shows nothing of that kind changed there.
    for said in ("In 2.0, a maximum of 9 was added to `name`.", "In 2.0, `name` in `Pet` gained a pattern.",
                 "In 2.0, `name` has added a maximum of 9."):
        assert [f.kind for f in verify(said, rows).findings] == ["unsupported-claim"], said


def test_formats_and_what_a_field_lacks_are_not_claims_that_it_is_absent(write):
    spec = """
        paths:
          /pets:
            get:
              responses: {'200': {description: ok, content: {application/json: {schema: {$ref: '#/components/schemas/Pet'}}}}}
        components:
          schemas:
            Pet:
              properties:
                age: {type: integer%s}
                born: {type: string, format: date}
        """
    root = write({"a/openapi.yaml": HEAD + dedent(spec % ""),
                  "b/openapi.yaml": HEAD + dedent(spec % ", minimum: 0, maximum: 30")})
    s = SpecSet()
    s.load(root / "a/openapi.yaml", "1.0")
    s.load(root / "b/openapi.yaml", "2.0")
    ev = [s.compare("Pet.age", "1.0", "2.0"), s.get_schema("Pet", "1.0"), s.get_schema("Pet", "2.0")]
    # A date or time format in backticks is not a name.
    assert verify("`born` is a date written as `YYYY-MM-DD`, a time as `HH:MM:SS`.", ev).ok
    # What "it" has none of says nothing about whether it is there.
    assert verify("`age` now has a minimum and a maximum in 2.0, whereas in 1.0 it had no constraints.", ev).ok
    assert verify("`age` is an integer in 1.0. In 1.0 the spec does not define any mechanism for securing it.",
                  ev).ok
    # It missing is still a claim, and wrong here.
    assert kinds(verify("`age` is an integer in 2.0. In 1.0 it is missing.", ev)) == [
        ("contradicted-claim", "In 1.0 it is missing.")]
    assert kinds(verify("`born` uses the `dateOfBirth` field.", ev)) == [("unknown-name", "dateOfBirth")]
