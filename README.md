# speclens

Grounded, citable answers about API specifications, for LLMs and for people.

speclens parses an OpenAPI spec (multi-file, with every `$ref` resolved) into a graph of operations, schemas and
fields. Each node keeps its source file and line numbers. A component kept in another file
(`Pet: {$ref: './schemas.yaml#/Pet'}`) is named as the component. You can then ask exact questions about it:
- every field of a schema, including the ones it inherits, each with its source;
- where a field is used;
- what changed between two versions;
- the original YAML behind any answer.

It can also check an LLM's answer against the evidence the model was given.

**Status:** early development. Loading, `$ref` resolution, the graph, the queries, comparing two specs and
checking answers work.

## Why speclens, and when to use something else

speclens is not a better diff tool. Diff tools such as [oasdiff](https://github.com/oasdiff/oasdiff) answer
one question for a pipeline: does this new version break clients? speclens answers the questions people ask an
assistant about a spec, exactly and with a citation for each line, and then checks the assistant's answer.

| You want to | Use |
|---|---|
| Fail a CI build when a release breaks clients; changelogs; many checks with configuration | oasdiff |
| "What fields does `Beverage` have, including inherited ones, and where is each written?" | speclens `get_schema` |
| "Where is `price` used?" / "Which schemas does this question mean?" | speclens `find_field`, `names_in` |
| "What changed in `Pet` (or `GET /pets`, or `Pet.name`) between 1.2 and 1.10?" | speclens `compare` |
| Check that an LLM's answer cites real ids, names real fields, counts right, gets versions right and leaves out no breaking change | speclens `verify` |
| Give an LLM those tools | speclens MCP server, or the Python library in your own pipeline |

What that gives you over a diff tool or plain text search:
- **Answers from the resolved spec, not from chunks of YAML.** A field inherited through `allOf`, or defined in
  another file behind a `$ref`, is in the answer with its source. Text search over chunks of a spec misses it.
- **A citation on every line**: an id and the file and lines it came from, so a person can check it and
  `source` shows the YAML behind it.
- **One thing at a time.** `compare` answers for one schema, operation or field, in the same ids as every other
  answer, so a claim about versions can be checked against it. A diff tool reports the whole spec.
- **Checking the answer.** `verify` reports what the evidence does not support; a diff tool has nothing like it.
- **A plain Python library**: one dependency (PyYAML), no service, no network.

### How it was tested

On public specs only (APIs.guru samples, every OpenAPI 3.1 spec there, Stripe, GitHub, DigitalOcean and the
OpenAPI Initiative examples). The reading and comparing checks can be rerun with the scripts in `tools/`; the
answer-checking numbers were measured with scripts not included in this repository.
- **Reading specs:** 524 public specs, each checked against an independent naive reading
  (`tools/crosscheck.py`, `tools/sample_public_apis.py`).
- **Comparing versions**, against oasdiff 1.34.1 (`tools/compare_with_oasdiff.py`): on 28 release pairs from
  10 vendors, the same operations rated breaking on all 27 that oasdiff can read. On GitHub v1.1.3 to v1.1.4,
  oasdiff with `--flatten-allof` reports 25 more operations, all from a `nullable` next to `allOf` that it
  drops; without that flag it misses changes behind `allOf`, which speclens finds. On security and operationIds,
  over 31 release pairs: the same 46 operations that lost anonymous access and the same 20 renamed operationIds.
- **Checking answers:** 4,207 claims about versions in 20 phrasings on real release pairs: every false one
  reported, no true one reported. 948 answers written in many styles (code, JSON, tables, URLs, product names)
  on about 510 specs, and 1,768 sentences that negate a property of a field ("`x` has no default"): no false
  alarms. For omitted changes: 481 complete answers with no finding, and 396 of 396 dropped changes reported.

Limits: Swagger 2.0 and remote `$ref` are not read; `verify` reads English and checks only claims written in
the patterns it knows; `compare` covers the common changes to schemas, parameters, responses, security and
operationIds, fewer kinds than oasdiff.

## Install

speclens needs Python 3.11 or newer; its only dependency is PyYAML. It is not on PyPI yet: install it from a
clone of this repository (`pip install .`, or `pip install '.[mcp]'` for the MCP server), or from a wheel built
with `uv build`.

## Use

```
uv sync
uv run speclens validate openapi.yaml                    # broken references and other problems, with file:line
uv run speclens get-schema openapi.yaml Beverage         # every field, inherited ones with their source
uv run speclens get-operation openapi.yaml "GET /orders/{orderId}"
uv run speclens find-field openapi.yaml price            # where it is written, who holds it, who uses it
uv run speclens list-operations openapi.yaml --filter order
uv run speclens search openapi.yaml "revenue statistics"
uv run speclens names-in openapi.yaml "what fields does a menu item have?"
uv run speclens source openapi.yaml "1.0.0:field/Beverage.volume"
```

Every command takes `--json`; query commands also take `--full`. Text output cuts long descriptions to keep
it short; `--full` (or `to_text(full=True)`) shows them whole, and `--json` / `to_dict()` always has them
whole. `get-schema` and `get-operation` take `--expand N` (`expand=N` from Python and MCP): the named schemas
they refer to (a body's schema, the schemas fields hold, through arrays and options), each as a result of its
own, N levels deep, each schema once. An operation's error responses (4xx, 5xx, default) are left out unless
`--errors` (`errors=True`). `--max-chars N` keeps the text within N characters by leaving out expanded schemas,
the deepest first; each is then listed by name. From Python:

```python
from speclens import load_spec

spec = load_spec("openapi.yaml", label="v1")
print(spec.get_schema("Beverage").to_text())
```

```
schema Beverage [v1:schema/Beverage components/schemas/Beverage.yaml:1-19]
type: object
fields (12: 3 own, 9 inherited):
  category: string const 'beverage', required [v1:field/Beverage.category components/schemas/Beverage.yaml:4-7] — Menu item category.
  volume: number, exclusiveMinimum 0, required [v1:field/Beverage.volume components/schemas/Beverage.yaml:8-11] — Size of the beverage in milliliters.
  ...
  inherited from MenuBaseItem (9):
    ...
    price: integer, minimum 0, required [v1:field/MenuBaseItem.price components/schemas/MenuBaseItem.yaml:29-32] — Price in cents.
    ...
used by 3 operations: GET /menu, GET /order-items, POST /menu
```

Field and parameter rows show the limits on the value as written, on it or on the schema it is through
`$ref` and `allOf` (`string, maxLength 3`; `limits` in JSON): `minLength`, `maxLength`, `minimum`,
`maximum`, the exclusive bounds, `minItems`, `maxItems`, `multipleOf`, and the `pattern` the value must match.
A field or parameter whose type is a named schema names it (`Coordinates`,
`ShipmentStatus (string enum [...])`), so the next schema to look up is clear. The default text shows an enum's
first 8 values; `full=True` and JSON (`enum`) have them all, except that in `full=True` a row whose enum of
more than 20 values is held in a named schema shows the first 8 and "… N more in Country": the schema's own text
lists them all, once, instead of on every row that holds it. A schema with a discriminator shows its mapping
(value -> schema), for allOf parents as for oneOf, and a parent lists the schemas that extend it with allOf.
`get_operation` also shows a response's links (to another operation, by `operationId` or `operationRef`, with the
values passed) and the operation's callbacks (name, the expression giving the URL, method, summary, and the schema
of each body sent, which counts as used by the operation).

`search` is ranked keyword search for when no name is known: BM25F, which scores a node's name, summary,
description and other text (operationId, tags) apart, each with its own weight and length normalisation, so a long
description does not bury a name. The name is a schema's or parameter's name, a field's own name (not its
schema's), or an operation's path without its parameters; a node whose whole name is the query scores more. A
last part holds what a node belongs to (a field's schema, a parameter's operation, a schema itself), so "what does
`id` mean on an owner" finds `Owner.id` and "what fields does an owner have" finds `Owner`. A path written in the
query (`GET /owners/{ownerId}`) puts the operations with exactly that path first. "Get owner by id" (or "delete
an owner by its ID", "update owner by ownerId") puts first the operations on one owner: the method the verb says,
on a path ending in the parameter after `owners` (`GET /owners/{ownerId}`, not `/owners/{ownerId}/pets/{petId}`).
It needs a verb it knows (get, fetch, update, delete and the like) and an id that is the item's own, so "pets by
ownerId" is left alone. A query that reads like a
description, most of its words in one node's description, is ranked by plain BM25 over all of a node's text
instead, which ranks such sentences best. The weights and this rule were set on half of a sample of public specs
and checked on the other half,
where every kind of question ranks as well as plain BM25 or better, to within 0.01.

Each mention `names_in` finds says what it resolved to (`name`) and how it was written (`strength`): `exact`
for a path, a quoted name or one shaped like code, `phrase` for two or more plain words, `capitalised` for
a single capitalised word ("Card", which usually means the schema, but may only start a sentence), and `word`
for a single lowercase word, which may be an everyday word that only happens to be a name ("order"). What to
trust is the caller's choice. A parameter is found from plain words of two or more ("page size" for
`page_size`), a header also without its `x-` ("request id" for `X-Request-Id`). Words followed by "header" or
"parameter" name the parameter, not a schema of the same name that is only its type ("the actor type header"
for `Api-Actor-Type`, whose type is `ActorType`), and may be the end of its name. A name that is both a field and a parameter
(`orderId`) is both. A name of two words or more that is the last fixed segment of a path names the operations
there ("the estimateShipping call" for `POST /estimateShipping`, `pet_owners` for `GET /pet_owners/{id}`),
unless it names a schema or operation already; one quoted word does when it names nothing else ("`captures`"),
and a version segment ("v1") never does. So do plain words that spell such a name, also with "a", "an" or "the"
between ("how do I estimate the shipping?"), beside what else the words name. Such a mention has `how` "path
segment".
Plain words spelling an operationId of two words or more name that operation the same way, with its verb in
any form ("book a shipment", "booking a shipment" for `bookShipment`), as `how` "operationId", beside what
else the words name and never instead of it. A verb form after a determiner ("a", "the", "each", ...) or after
another verb the spec's operationIds start with describes a thing and names no operation: "cancel a scheduled
order" is not `scheduleOrder`. Words that end in a possessive belong to the words after it:
"getting a pet's medical record" names `getPetMedicalRecord` or `getPetRecord`, never `getPet`. Neither kind
of phrase runs across a comma or a full stop.

An operation written loosely is found too, as `how` "path (loose)" and `strength` `phrase`: a path with braces
left out or a value in their place ("get /pets/petId", "get /pets/42" for `GET /pets/{petId}`), and a method with
one to three words for its last fixed segment and "endpoint", "operation", "route", "call" or "request" ("the get
pets endpoint" for `GET /pets`, "get pet endpoint" for `GET /pets/{petId}`; the shortest such path). A method
the path does not have ("delete /pets/{petId}" where only GET is) gives the methods it does have, as `how`
"path (other method)", so an answer can say which exist. A path may hold any character a path segment can
(`/pets/$count`, `/vets(name={name},city={city})`, and the `#` and `=` some converted specs use); punctuation
the sentence puts after it is not part of it.

An API split into several root documents (one per area, sharing a components file) loads as one spec: pass
the folder, or a list of root files. The roots are the folder's files with a top-level `openapi` key; a path
defined in two roots, or roots of different OpenAPI versions, are reported.

```python
spec = load_spec("api/", label="2.0")                       # every root file in api/
spec = load_spec(["api/pets.yaml", "api/stores.yaml"])      # or just these
```

In OpenAPI 3.1, references follow JSON Schema 2020-12: `$id` sets the base URI that references inside it are
read against (URNs included), `$anchor` and `$dynamicAnchor` name a place to refer to, and a `$dynamicRef` that
could land on several schemas keeps each as a condition, so their fields are never counted as all fields.
References to other servers are reported, not fetched.

Specs from elsewhere: `load_spec(path, allow=[folder])` reads no file outside those folders, even one a `$ref`
names (reported as `outside-allowed`). A file nested more than 200 levels deep, or whose YAML aliases repeat
parts of it into more than 100,000 values, is refused as `too-large` instead of read; real specs stay far below
both.

## Compare two specs

Two versions of an API, or a branch and its main line. This answers questions about one thing at a time, in
the same ids and citations as every other answer, so an LLM's claims about versions can be checked:

```
uv run speclens compare old.yaml new.yaml Pet               # a schema, an operation ("GET /pets") or a field (Pet.name)
uv run speclens diff old.yaml new.yaml                      # everything, breaking changes first
uv run speclens diff old.yaml new.yaml --breaking           # exit 1 if anything breaks
```

A diff's text starts with what looks renamed: a schema whose fields or values are mostly kept; a field whose
name changed only in case or separators; a field that is the only one removed and the only one added in its
schema, of the same type (`eta` -> `estimatedArrival`); and a parameter that is the only one removed and the
only one added in its place (query, header, ...) of an operation, listed once with every operation it is in
(`page` -> `cursor`, the types may differ). These are hints, each with its reason. Then each distinct breaking
change once, in the first operation it breaks, with the other operations it breaks; then what was added and
removed, and a count of the other changes. `full=True` lists every breaking change and those other changes too,
by schema and by operation, and JSON always has every change.

The same thing written another way is no change: OpenAPI 3.0's `maximum: 5` with `exclusiveMaximum: true` and
3.1's `exclusiveMaximum: 5` are one bound, and `enum: [x]` and `const: x` allow the same value.

Each change is rated for the side it affects: removing a required field breaks those who read responses,
adding a required field breaks those who send requests. The ratings follow the defaults of
[oasdiff](https://github.com/oasdiff/oasdiff), and `tools/compare_with_oasdiff.py` runs both on two specs and
prints every operation where they disagree.

Security is compared as a client meets it. An operation's requirement (its own `security`, else the document's)
lists ways in, any one of which is enough; nothing declared, or `{}`, is anonymous access. Removing anonymous
access or a way in, or requiring another scope, breaks; adding a way in or dropping a scope does not. A scheme
whose type or credential location changes breaks every operation that uses it. `get_operation` shows each
operation's requirement, or "security: none declared" when the spec gives none. `validate` warns about a scheme
a requirement names that is not defined, and about a scheme no requirement names. A renamed
`operationId` is info: the HTTP call is the same, but code generated from the spec is renamed.

`diff --breaking` is there for quick checks; to gate releases, use oasdiff, which has far more checks
(sunset dates, extensions and more) and configuration. What `compare` adds is answers about one thing,
in citable ids that `verify` can check (see [Why speclens](#why-speclens-and-when-to-use-something-else)).

Names can be given as written (`MenuItem`), in any case, or in plain words (`menu items`). A name that matches
nothing raises `NotFound` with the closest names; one that matches several raises `Ambiguous`.

## Check an answer

Give `verify` an LLM's answer and the evidence the model was shown: speclens results, plain text, or
`(label, text)` pairs such as search chunks from a known version.

```python
from speclens import SpecSet, verify

specs = SpecSet()
specs.load("v1/openapi.yaml", "1.2")
specs.load("v2/openapi.yaml", "1.10")
evidence = [specs.get_schema("Pet", "1.2"), specs.get_schema("Pet", "1.10")]
print(verify("In 1.2 the Pet schema is missing `name`, which 1.10 has.", evidence).to_text())
```

```
checked 0 citations, 1 names, 1 version claims, 0 counts: 1 finding
  contradicted-claim: 'In 1.2 the Pet schema is missing `name`' — the evidence shows name in Pet in 1.2
```

It reports:
- `unknown-citation`: an id the answer cites that is not in the evidence;
- `unknown-name`: a name written as code that appears nowhere in the evidence (JSON Schema's own keywords,
  such as `maxLength` or `minItems`, are never unknown, nor a family such as `Pet*` when a name begins so, nor
  a date or time format such as `YYYY-MM-DD` or `HH:MM:SS`);
- `contradicted-claim`: a claim that something is missing, added or removed in a version, which the evidence
  contradicts; or that a field or parameter is required, or optional, when the evidence shows the other; or
  a limit on its value when the evidence shows that limit with another value: "`nickname`: maximum 40
  characters", "`limit` is at least 1", "up to 5 items", "between 2 and 10 characters", "exactly 3
  characters", "a `maxLength` of 40", "the maximum length of `nickname` in `Owner` is 40", "the minimum `age`
  is 0", "at least one `tag`", "a minimum of 1 and a maximum of 50", "values from 0 to 120", or a list under
  the name ("`age`:", "Field: `age`" or "The `age` field has these limits:", then "- Maximum: `120`"); or that
  a value has no limit ("`nickname` has no maximum length", "there is no maximum length for `nickname`") when
  the evidence shows one. The name may have words between it and the limit (a version, "in Acme 2.0", "in the
  `Owner` schema", "is an integer (`int32`) with"), come after it ("a minItems 1 constraint on the `tags`
  array"), be "it", "the field" or "this means the value" for the one name with limits in the sentence or list before,
  or be plain words that spell exactly one field of the schema named ("the minimum age in `Owner`"). Under a
  heading that names one version ("#### Version 1.0"), a claim with no version of its own is checked against
  that version only. A limit the evidence does not show, which may be in a description only, is not checked, nor one
  said roughly ("about 100") or only sometimes ("when ...");
- `unsupported-claim`: such a claim, or one that something changed, that nothing in the evidence supports
  (one finding per claim, whatever number of names and versions it is about);
  something added inside a field ("`lost` was added to the `status` enum", "`age` now has a minimum of 0", "a
  pattern was added to `code`") is a change in that field, not the field added: it needs a comparison showing
  that kind of change there (a change to a schema two fields share counts for both), and is not checked when no
  comparison covers the field;
- `wrong-count`: a number of fields, parameters or options stated for a schema or operation whose complete
  list is in the evidence, that matches none of its counts;
- `omitted-change`: with `cover=[comparisons or diffs]`, a breaking change in them that the answer never
  mentions, by the name of what changed (`kind`, or "birth date" for `birthDate`), a value added or removed
  (`application/zip`), a security scheme, or for options unwrapped or wrapped, a key or schema of them or the
  word "wrapped". A header written in words counts without its `x-` ("trace id" for `X-Trace-Id`). Naming only
  the operation or schema is not mentioning the changes inside it, though a longer name that starts with it is
  (`PetCat` in a comparison of `Pet`). A value counts as written, or by its plain key only when no other changed
  value has that key (`GIFT_CARD` and `GIFTCARD` are two values). Naming a field covers its changed values,
  unless the answer names some of them: then each one it leaves out is reported. Each such finding gives in
  `places` every schema or operation the change was reached from (the detail names the first and counts the rest).

In a conversation, pass the earlier turns as `known=[...]`: names repeated from them are not `unknown-name`,
but they never support a citation, a count or a claim.

```python
c = specs.compare("POST /pets", "1.2", "1.10")
verify(answer, [c], cover=[c], known=[previous_answer])
```

It reports and never rewrites. Claims are found by plain English patterns ("missing in", "lacks", "added in",
"since", "no longer", "removed", "is gone", "changed", ...) in a sentence that names a version label from the
evidence; a claim worded another way is not checked, and the meaning of prose is not judged. A bare number as a
label counts only after "version" or "v" ("version 2", "v2"), since numbers are everywhere in prose. A
negation is a claim that something is absent only when it is that thing that is negated: "there is no `tag`"
and "Pet lacks `tag`" are, "no encryption is applied to `tag`", "`tag` has no default" and "no tag guidance"
are not. The same holds for "it": "1.2 lacks it" is a claim, "it had no constraints" and "no mechanism for
securing it" are not. In "`kind` gained `VOUCHER`" or "Pet now has `age`", the name before the verb holds the new
thing and is not what is new. A plain word is a subject only if the evidence has it as a name (an id, a result, a YAML or JSON key,
code in backticks), never only in its prose, and never a function word such as "are" or "such". "It" and
"these" refer back only to a name the evidence has, and a reference in brackets (`[3]`, `[chunk_7]`) is where a
statement comes from, never what it is about. A path in backticks (`pets[].tag`) is one name, checked against
the comparisons' changes at that path. In a claim that something was removed, a version named as where it still
was ("removed (was a string in 1.2)") stands for the version after it. An operation written as method and path
("DELETE /pets/{id}", path templates by place) is one name, the subject only when nothing else is named ("GET
/pets has no `tag`" is about tag): saying it does not exist is contradicted by that method on that path, not by
another method there, and supported by a `names_in` result that gives the methods the path has. A comparison of
the thing itself backs "Pet changed in 1.10". A kind written before a name ("schema:Pet") and a word of a version
label ("streaming" in 0.0.0-streaming) are not names. Outside
backticks only camelCase and snake_case words are checked as names, so product names such as JavaScript or
GitHub are not findings; a name containing spaces is not checked.

Required and optional are checked for the name the word is said of ("`limit` is optional", "a required
`X-Trace` header", and for a path such as `pets[].tag`, its last name), against fields and parameters in
results and comparisons, in the version the sentence names, or else the heading above it ("#### Version 1.0");
with none named, in the latest version when it says "new", "now", "became" or the like, otherwise in every version
when they agree. A record also counts: "- Field: `limit`", then "- Required: Yes". A condition ("required when paying by card")
leaves it alone, and so do text chunks. A plain word after "required" or "optional" that names being required
("the required status changed", "optional flags", "required settings") is not a field, unless it is written as
code or followed by "field", "property", "parameter" or the like ("a required status field").

Counts are checked only when a number stands right before "fields", "properties", "parameters", "options" (and
the like), and the sentence names a schema or operation whose full result is in the evidence, or says "it" or
"the schema" when there is only one. An unqualified count may be all fields or only the schema's own; "inherited",
"required", "optional", "own", "query" and "inherits N fields from X" are checked as such. Approximate or partial
counts ("at least 3", "the first 2", "4 more than") and counts of changes ("3 fields were added") are left alone,
and nothing is counted from text chunks or from a schema whose fields depend on oneOf/anyOf options.

## Use from an LLM (MCP)

`speclens-mcp` serves the queries, `compare` and `verify` as tools over MCP (stdio), for any MCP client. It
needs the `mcp` extra (`uv sync --extra mcp`, or `pip install 'speclens[mcp]'`).

```
uv run speclens-mcp v1/openapi.yaml=1.2 v2/=1.10          # files or folders, each under a label
uv run speclens-mcp api/ --allow ~/specs                  # load_spec may load more, only from under ~/specs
```

A client config, for example:

```json
{"mcpServers": {"speclens": {"command": "uv", "args": ["run", "--project", "/path/to/speclens", "--extra", "mcp",
                                                        "speclens-mcp", "/path/to/openapi.yaml"]}}}
```

`--project` runs speclens's environment from the client's own folder, so a relative spec path is read from there
(`--directory` would read it from the speclens folder instead).

The tools are `list_specs`, `names_in`, `get_schema`, `get_operation`, `find_field`, `list_operations`,
`search`, `source`, `compare`, `diff`, `validate`, `verify` and `clear_evidence`, plus `load_spec` with
`--allow`, which reads nothing outside those folders. All but `load_spec` are read-only. The server tells the model how to use them: `names_in` first,
`search` only when it finds no name, and `verify` on the draft before answering. `verify` (with `known` for earlier turns, and
`cover_changes` to report breaking changes the draft leaves out) checks against
every result the server returned since it started (or since `clear_evidence`), as the model was shown it
(text cut short is evidence only as far as it goes), so the evidence is what the model was actually shown.

## Develop

```
uv run pytest
uv run python tools/crosscheck.py path/to/openapi.yaml   # compare the graph with an independent naive reading
uv run python tools/sample_public_apis.py /tmp/apis       # run that on a random sample of public APIs
```

## License

MIT; see [LICENSE](LICENSE). The test fixtures under `tests/fixtures/` keep their own licenses, listed in
[tests/fixtures/README.md](tests/fixtures/README.md).
