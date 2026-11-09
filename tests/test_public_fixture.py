"""A public multi-file spec (see fixtures/README.md) and the `speclens validate` command."""

import json
from pathlib import Path

from speclens import load_spec
from speclens.cli import main

STARTER = Path(__file__).parent / "fixtures" / "openapi-starter" / "openapi.yaml"


def test_public_multi_file_spec_loads_with_no_issues():
    s = load_spec(STARTER, "v1")
    assert s.issues == []
    assert s.version == "3.1"
    assert len(s.documents) > 30
    g = s.graph
    # A schema in a file of its own is named by the file.
    assert g.nodes["v1:schema/MenuItem"].file == "components/schemas/MenuItem.yaml"
    options = {e.target: e.data["values"] for e in g.outgoing("v1:schema/MenuItem", "option")}
    assert options == {"v1:schema/Beverage": ["beverage"], "v1:schema/Dessert": ["dessert"]}
    # A path item in a file of its own is named by its path.
    assert "v1:schema/PATCH /orders/{orderId}#request/application/json" in g.nodes
    # Inherited fields reach the options.
    inherited = [e for e in g.outgoing("v1:schema/Beverage", "has_field") if e.data["inherited"]]
    assert {g.nodes[e.target].data["name"] for e in inherited} >= {"id", "name", "price"}


def test_cli_validate(capsys):
    assert main(["validate", str(STARTER)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("1.0.0 (OpenAPI 3.1):") and out.rstrip().endswith("ok")
    assert main(["validate", str(STARTER), "--json", "--label", "v1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["label"], report["ok"], report["issues"]) == ("v1", True, [])


def test_cli_validate_fails_on_a_broken_reference(tmp_path, capsys):
    root = tmp_path / "openapi.yaml"
    root.write_text("openapi: 3.0.3\ninfo: {title: t, version: '1'}\n"
                    "components:\n  schemas:\n    A: {$ref: '#/components/schemas/B'}\n")
    assert main(["validate", str(root)]) == 1
    out = capsys.readouterr().out
    assert "error [unresolved-ref] openapi.yaml:5:" in out
    assert out.rstrip().endswith("1 error")


def test_independent_crosscheck_finds_no_differences(capsys):
    import importlib.util
    path = Path(__file__).parent.parent / "tools" / "crosscheck.py"
    module_spec = importlib.util.spec_from_file_location("crosscheck", path)
    crosscheck = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(crosscheck)
    assert crosscheck.check(STARTER) == []
