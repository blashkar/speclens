# Contributing

Issues and pull requests are welcome.

## Setup

speclens needs Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```
uv sync --extra mcp
uv run pytest -q
```

## Pull requests

- CI must pass on Python 3.11, 3.12 and 3.13. `main` accepts changes only through pull requests.
- Add a test that fails without your change.
- Keep speclens general: a change should follow from OpenAPI, JSON Schema or plain English, not from the wording
  or names of one particular API. Check it on more than one spec.
- Tests use synthetic specs or specs under a permissive licence, with the licence kept next to them. Do not add
  specs you are not free to redistribute.
