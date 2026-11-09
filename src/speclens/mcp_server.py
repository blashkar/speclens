"""An MCP server: the queries, compare and verify as tools an LLM can call.

    speclens-mcp SPEC[=LABEL] [SPEC[=LABEL] ...] [--allow DIR ...]

Each SPEC is a root file, a folder of root files, or several root files joined with commas; the label
defaults to info.version. With `--allow`, the `load_spec` tool can load more specs, but only from under those
folders. Needs the `mcp` extra: `pip install 'speclens[mcp]'`.

The server keeps every result it returns (the last MAX_EVIDENCE), and `verify` checks an answer against them
by default: the evidence is what the model was shown. One server serves one client over stdio; a client that
runs several conversations on one server should call `clear_evidence` between them.
"""

import argparse
import sys
from pathlib import Path

from .compare import Comparison, SpecDiff
from .query import Ambiguous, NotFound
from .spec import SpecSet, load_spec
from .verify import verify as _verify

MAX_EVIDENCE = 200

INSTRUCTIONS = """\
Exact, cited answers about the OpenAPI specs loaded here. Every answer line carries an id in brackets, such as
[v1:schema/Pet components/schemas/Pet.yaml:1-19]: cite those ids, and say only what the results show.

How to use the tools:
1. `names_in` first, with the user's question: it finds the schemas, fields, operations and parameters the
   question names, exactly, without guessing. Then ask about those names.
2. `get_schema` for all fields of a schema (inherited ones included, with their source), `get_operation` for
   an operation, `find_field` for where a field is written and who uses it, `list_operations` for an overview.
3. `search` only when `names_in` finds nothing: it is ranked keyword search, not exact.
4. Two versions: `compare` for one schema, operation or field, `diff` for everything.
5. Before answering, `verify` your draft: it reports ids, names, counts and version claims that the results
   you were given do not support. Fix those, do not argue with them.
`list_specs` shows the loaded specs and their labels; give `label` when more than one is loaded."""


def build_server(specs: SpecSet, allow: list[Path] | None = None):
    """The MCP server over `specs`. `allow` lists the folders `load_spec` may read; without it the tool is not
    offered."""
    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
        from mcp.types import ToolAnnotations
    except ImportError as e:  # pragma: no cover
        raise SystemExit("the MCP server needs the mcp extra: pip install 'speclens[mcp]'") from e

    server = MCPServer("speclens", instructions=INSTRUCTIONS)
    evidence: list = []
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    allowed = [Path(p).resolve() for p in allow or ()]

    def run(make):
        try:
            return make()
        except (NotFound, Ambiguous, KeyError, ValueError) as e:
            raise ToolError(str(e).strip("'\"")) from e

    def shown(item):
        """Keep what the model is shown, as `verify` evidence."""
        evidence.append(item)
        del evidence[:-MAX_EVIDENCE]

    def answer(make, full: bool = True) -> str:
        result = run(make)
        text = result.to_text(full)
        shown(result if full else text)  # cut text is evidence only as far as it goes
        return text

    def tool(fn):
        server.add_tool(fn, annotations=read_only, structured_output=False)
        return fn

    @tool
    def list_specs() -> str:
        """The loaded specs: label, OpenAPI version, size and problems. Labels sort as versions (1.10 after 1.2)."""
        if not specs.specs:
            return "no specs loaded"
        lines = []
        for label in specs.labels():
            s = specs.get(label)
            c = s.counts()
            lines.append(f"{label}: OpenAPI {s.version or '?'}, {c['operations']} operations, {c['schemas']} schemas, "
                         f"{c['fields']} fields; {len(s.errors)} errors, {len(s.issues) - len(s.errors)} warnings")
        shown("\n".join(lines))
        return "\n".join(lines)

    @tool
    def names_in(text: str, label: str | None = None) -> str:
        """The spec's names that a question mentions: schemas, fields, operations, paths and parameters, written
        as identifiers or in plain words ("billing address" -> billingAddress). Exact, no guessing: call it first
        with the user's question."""
        return answer(lambda: specs.names_in(text, label))

    @tool
    def get_schema(name: str, label: str | None = None, full: bool = True, expand: int = 0,
                   max_chars: int | None = None) -> str:
        """Every field of a schema, inherited ones with the schema they come from, oneOf/anyOf options, and the
        operations that use it. `name` as written (Pet), in another case, in plain words (pet owners), or an id.
        `full=False` cuts long descriptions. `expand=1` adds the schemas its fields hold; `max_chars` leaves out the
        deepest of them, by name, beyond that size."""
        return answer(lambda: specs.get_schema(name, label, expand, max_chars), full)

    @tool
    def get_operation(operation: str, label: str | None = None, full: bool = True, expand: int = 0,
                      errors: bool = False, max_chars: int | None = None) -> str:
        """Parameters, request body and responses of one operation: "GET /pets/{petId}" (method and exact path)
        or an operationId. `expand=1` adds the schemas of its request and response bodies (`errors=True`: error
        responses' too); `max_chars` leaves out the deepest of them, by name, beyond that size."""
        return answer(lambda: specs.get_operation(operation, label=label, expand=expand, errors=errors,
                                                  max_chars=max_chars), full)

    @tool
    def find_field(name: str, label: str | None = None, full: bool = True) -> str:
        """Where a field (or property) is written, every schema that holds it, directly or inherited, and the
        operations that use those schemas. "Pet.name" for one schema's field."""
        return answer(lambda: specs.find_field(name, label), full)

    @tool
    def list_operations(label: str | None = None, filter: str | None = None) -> str:
        """Every operation, one line each. `filter` keeps those whose method, path, summary, operationId or tags
        contain the text."""
        return answer(lambda: specs.list_operations(label, filter), full=False)

    @tool
    def search(text: str, label: str | None = None, limit: int = 10) -> str:
        """Ranked keyword search (BM25) over names and descriptions. Not exact: use it only when `names_in` finds
        no name, then look the hits up with the other tools."""
        return answer(lambda: specs.search(text, label, min(max(limit, 1), 50)), full=False)

    @tool
    def source(id: str) -> str:
        """The original YAML or JSON lines behind an id from any result, with file and line numbers."""
        return answer(lambda: specs.source(id))

    @tool
    def compare(name: str, old_label: str, new_label: str) -> str:
        """What changed in one schema, operation or field (Pet.name) from the spec labelled `old_label` to the one
        labelled `new_label`, each change rated breaking or not for the side it affects."""
        return answer(lambda: specs.compare(name, old_label, new_label))

    @tool
    def diff(old_label: str, new_label: str, breaking_only: bool = False) -> str:
        """Everything that changed between two loaded specs, breaking changes first. `breaking_only` lists only
        the breaking ones, by operation."""
        if not breaking_only:
            return answer(lambda: specs.diff(old_label, new_label))
        broken = run(lambda: specs.diff(old_label, new_label)).breaking()
        text = (f"{len(broken)} breaking changes from {old_label} to {new_label}:\n" +
                "\n".join(f"  {op}: {c.line()}" for op, c in broken)) if broken else "no breaking changes"
        shown(text)  # not the whole diff: the model saw only these
        return text

    @tool
    def validate(label: str | None = None, limit: int = 50) -> str:
        """Broken references and other problems in a spec, with file:line. Answers about a spec with errors may be
        incomplete."""
        try:
            s = specs.get(label)
        except KeyError as e:
            raise ToolError(str(e).strip("'\"")) from e
        lines = [f"{s.label}: {len(s.errors)} errors, {len(s.issues) - len(s.errors)} warnings"]
        lines += [i.to_text() for i in s.issues[:limit]]
        if len(s.issues) > limit:
            lines.append(f"... and {len(s.issues) - limit} more")
        shown("\n".join(lines))
        return "\n".join(lines)

    @tool
    def verify(answer: str, evidence_text: list[str] | None = None, use_results: bool = True,
               known: list[str] | None = None, cover_changes: bool = False) -> str:
        """Check a draft answer before giving it: ids cited that no result showed, names that appear nowhere in
        the evidence, wrong counts of fields or parameters, and claims that something is missing, added, removed
        or changed in a version that the evidence contradicts or does not support. The evidence is every result
        this server returned (`use_results`), plus any `evidence_text`. `known`: earlier turns of the
        conversation, whose names the answer may repeat (they are not evidence). `cover_changes`: also report
        each breaking change in the compare and diff results shown that the answer does not mention. It reports
        and never rewrites; it does not judge prose."""
        ev = (list(evidence) if use_results else []) + list(evidence_text or [])
        if not ev:
            raise ToolError("no evidence: call the query tools first, or pass evidence_text")
        cover = [r for r in ev if isinstance(r, (Comparison, SpecDiff))] if cover_changes else None
        return _verify(answer, ev, known=known, cover=cover).to_text()

    @tool
    def clear_evidence() -> str:
        """Forget the results `verify` checks against, for example when a new conversation starts."""
        n = len(evidence)
        evidence.clear()
        return f"forgot {n} results"

    if allowed:
        def load(path: str, label: str | None = None) -> str:
            """Load another spec (a root file or a folder of root files) under a label, from the folders this
            server allows."""
            p = Path(path).expanduser().resolve()
            if not any(p == a or p.is_relative_to(a) for a in allowed):
                raise ToolError(f"{path} is not under a folder this server may read")
            if not p.exists():
                raise ToolError(f"{path} does not exist")
            try:
                # A $ref may point outside: such a file is never read, and the spec is not kept.
                s = load_spec(p, label, allow=allowed)
                if any(i.code == "outside-allowed" for i in s.issues):
                    raise ToolError(f"{path} references files outside the folders this server may read; not loaded")
                specs.add(s)
            except ValueError as e:
                raise ToolError(str(e)) from e
            text = f"loaded {s.label}: OpenAPI {s.version or '?'}, {len(s.errors)} errors"
            shown(text)
            return text

        server.add_tool(load, name="load_spec", structured_output=False,
                        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                                    open_world_hint=False))
    return server


def _parse(arg: str) -> tuple[object, str | None]:
    path, _, label = arg.rpartition("=")
    if not path or "/" in label or "\\" in label:  # no label: an "=" in the path itself
        path, label = arg, ""
    paths = path.split(",")
    return (paths if len(paths) > 1 else paths[0]), (label or None)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="speclens-mcp", description=__doc__.split("\n")[0])
    parser.add_argument("specs", nargs="*", metavar="SPEC[=LABEL]",
                        help="a root file, a folder of root files, or root files joined with commas")
    parser.add_argument("--allow", action="append", default=[], metavar="DIR",
                        help="a folder the load_spec tool may read (repeatable); without it there is no load_spec")
    args = parser.parse_args(argv)
    specs = SpecSet()
    for arg in args.specs:
        path, label = _parse(arg)
        spec = specs.load(path, label)
        if not spec.ok:  # stdout is the protocol: anything for people goes to stderr
            print(f"{spec.label} has {len(spec.errors)} error(s); run `speclens validate`", file=sys.stderr)
    if not specs.specs and not args.allow:
        parser.error("give at least one spec, or --allow a folder to load them from")
    build_server(specs, args.allow).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
