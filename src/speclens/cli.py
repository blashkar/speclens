"""Command line. Every command takes the spec's root file; text output by default, `--json` for scripts.

    speclens validate SPEC
    speclens get-schema SPEC NAME
    speclens get-operation SPEC "GET /pets/{id}"
    speclens find-field SPEC NAME
    speclens list-operations SPEC [--filter TEXT]
    speclens search SPEC TEXT
    speclens source SPEC NODE_ID
    speclens names-in SPEC "a question"
    speclens compare OLD_SPEC NEW_SPEC NAME      a schema, operation ("GET /pets") or field (Pet.name)
    speclens diff OLD_SPEC NEW_SPEC [--breaking]
"""

import argparse
import json
import sys

from .descriptions import description_issues
from .query import Ambiguous, NotFound
from .spec import SpecSet, load_spec

SHOWN_PER_CODE = 5


def validate(spec, args) -> int:
    errors = len(spec.errors)
    issues = spec.issues + description_issues(spec)
    if args.json:
        print(json.dumps({"label": spec.label, "openapi": spec.version, "ok": spec.ok, **spec.counts(),
                          "issues": [i.to_dict() for i in issues]}, indent=2))
    else:
        c = spec.counts()
        print(f"{spec.label} (OpenAPI {spec.version or '?'}): {c['files']} files, {c['operations']} operations, "
              f"{c['schemas']} schemas, {c['fields']} fields, {c['parameters']} parameters")
        shown: dict[str, int] = {}
        for issue in issues:
            shown[issue.code] = shown.get(issue.code, 0) + 1
            if args.all or shown[issue.code] <= SHOWN_PER_CODE:
                print(issue.to_text())
        for code, n in shown.items():
            if not args.all and n > SHOWN_PER_CODE:
                print(f"... and {n - SHOWN_PER_CODE} more [{code}] (--all shows every one)")
        warnings = len(issues) - errors
        print("ok" if spec.ok else f"{errors} error{'s' * (errors != 1)}", end="")
        print(f", {warnings} warning{'s' * (warnings != 1)}" if warnings else "")
    return 0 if spec.ok else 1


QUERIES = {
    "get-schema": (["name"], "every field of a schema, inherited ones with their source",
                   lambda s, a: s.get_schema(a.name, a.expand, a.max_chars)),
    "get-operation": (["operation"], "parameters, request body and responses of an operation",
                      lambda s, a: s.get_operation(a.operation, expand=a.expand, errors=a.errors,
                                                   max_chars=a.max_chars)),
    "find-field": (["name"], "where a field is written, which schemas hold it, which operations use it",
                   lambda s, a: s.find_field(a.name)),
    "list-operations": ([], "every operation, one line each", lambda s, a: s.list_operations(a.filter)),
    "search": (["text"], "keyword search over names and descriptions", lambda s, a: s.search(a.text, limit=a.limit)),
    "source": (["id"], "the original lines behind a node", lambda s, a: s.source_of(a.id)),
    "names-in": (["text"], "the spec's names a question mentions", lambda s, a: s.names_in(a.text)),
}


def query(spec, args) -> int:
    try:
        result = QUERIES[args.command][2](spec, args)
    except (NotFound, Ambiguous) as e:
        if args.json:
            print(json.dumps({"error": type(e).__name__, "message": str(e)}))
        else:
            print(e, file=sys.stderr)
        return 1
    print(json.dumps(result.to_dict(), indent=2, default=str) if args.json else result.to_text(args.full))
    return 0


def two_specs(args) -> int:
    old, new = load_spec(args.old, args.old_label), load_spec(args.new, args.new_label)
    if old.label == new.label:  # two releases with the same info.version, or none given
        old = load_spec(args.old, args.old_label or "old")
        new = load_spec(args.new, args.new_label or "new")
    specs = SpecSet()
    specs.add(old)
    specs.add(new)
    for spec in (old, new):
        if not spec.ok:
            print(f"{spec.label} has {len(spec.errors)} error(s); run `speclens validate` to see them", file=sys.stderr)
    try:
        result = specs.compare(args.name, old.label, new.label) if args.command == "compare" \
            else specs.diff(old.label, new.label)
    except (NotFound, Ambiguous) as e:
        print(json.dumps({"error": type(e).__name__, "message": str(e)}) if args.json else e,
              file=None if args.json else sys.stderr)
        return 1
    print(json.dumps(result.to_dict(), indent=2, default=str) if args.json else result.to_text())
    if args.command == "diff" and args.breaking and result.breaking():
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="speclens", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, help):
        p = commands.add_parser(name, help=help)
        p.add_argument("spec", help="the spec's root file, or a folder of root files")
        p.add_argument("--label", help="defaults to info.version")
        p.add_argument("--json", action="store_true", help="machine-readable output")
        p.add_argument("--full", action="store_true", help="descriptions in full, not cut")
        return p

    p = command("validate", "load a spec and report broken references and other problems")
    p.add_argument("--all", action="store_true", help="show every issue, not the first few of each kind")
    for name, (positionals, help, _) in QUERIES.items():
        p = command(name, help)
        for positional in positionals:
            p.add_argument(positional)
        if name == "list-operations":
            p.add_argument("--filter", help="only operations whose method, path, summary, operationId or tags "
                                            "contain this text")
        if name == "search":
            p.add_argument("--limit", type=int, default=10)
        if name in ("get-schema", "get-operation"):
            p.add_argument("--expand", type=int, default=0, help="also the schemas it refers to, this many levels")
            p.add_argument("--max-chars", type=int, help="leave out expanded schemas, deepest first, beyond this")
        if name == "get-operation":
            p.add_argument("--errors", action="store_true", help="with --expand, error responses' schemas too")
    for name, help in (("compare", "what changed in a schema, operation or field between two specs"),
                       ("diff", "everything that changed between two specs, breaking changes first")):
        p = commands.add_parser(name, help=help)
        p.add_argument("old", help="the older spec's root file or folder")
        p.add_argument("new", help="the newer spec's root file or folder")
        if name == "compare":
            p.add_argument("name")
        else:
            p.add_argument("--breaking", action="store_true", help="exit 1 if anything breaks")
        p.add_argument("--old-label", help="defaults to info.version, or 'old'")
        p.add_argument("--new-label", help="defaults to info.version, or 'new'")
        p.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if args.command in ("compare", "diff"):
        return two_specs(args)
    spec = load_spec(args.spec, args.label)
    if args.command == "validate":
        return validate(spec, args)
    if not spec.ok:
        print(f"the spec has {len(spec.errors)} error(s); run `speclens validate` to see them; answers may be "
              "incomplete", file=sys.stderr)
    return query(spec, args)


if __name__ == "__main__":
    sys.exit(main())
