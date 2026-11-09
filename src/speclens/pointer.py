"""JSON Pointer (RFC 6901), as used in `$ref` fragments."""

from urllib.parse import unquote


def escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def join(pointer: str, *tokens) -> str:
    return pointer + "".join("/" + escape(str(t)) for t in tokens)


def split(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"not a JSON pointer: {pointer!r}")
    return [unescape(t) for t in pointer[1:].split("/")]


def from_fragment(fragment: str) -> str:
    """A URI fragment holding a pointer is percent-encoded on top of the pointer escaping."""
    return unquote(fragment)


_MISSING = object()


def lookup(document, pointer: str):
    """The value at `pointer`, or raises KeyError naming the first token that is missing."""
    value = document
    for token in split(pointer):
        if isinstance(value, dict):
            value = value.get(token, _MISSING)
        elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
            value = value[int(token)]
        else:
            value = _MISSING
        if value is _MISSING:
            raise KeyError(token)
    return value
