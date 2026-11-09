import textwrap

import pytest

from speclens import load_spec


@pytest.fixture
def write(tmp_path):
    """Write files (name -> YAML text) under a temporary folder; returns the folder."""
    def write(files: dict[str, str]):
        for name, text in files.items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
        return tmp_path
    return write


@pytest.fixture
def spec(write):
    """Load a spec from files; the root is `openapi.yaml`."""
    def spec(files: dict[str, str], label="v1"):
        return load_spec(write(files) / "openapi.yaml", label)
    return spec


def codes(spec):
    return [i.code for i in spec.issues]
