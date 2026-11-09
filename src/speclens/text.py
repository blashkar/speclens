"""Words from identifiers and prose, the same way everywhere: names, search, and finding names in a question."""

import re

_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_STOP = {"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of", "on", "or",
         "that", "the", "this", "to", "with", "what", "which", "does", "do", "how", "i", "me", "my"}


def words(identifier: str) -> list[str]:
    """`PetOwner` -> pet, owner; `petId` -> pet, id; `HTTPError_code` -> http, error, code."""
    return [w.lower() for w in _WORD.findall(identifier)]


def key(text: str) -> str:
    """A name with case and separators removed: `Pet Store`, `pet_store` and `PetStore` share one key."""
    return "".join(words(text))


def variants(k: str) -> set[str]:
    """A key and its singular forms, so `dogs`, `statuses` and `categories` find `Dog`, `Status`, `Category`."""
    out = {k}
    if k.endswith("ies") and len(k) > 4:
        out.add(k[:-3] + "y")
    if k.endswith("es") and len(k) > 3:
        out.add(k[:-2])
    if k.endswith("s") and len(k) > 2:
        out.add(k[:-1])
    return out


def stem(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def terms(text: str) -> list[str]:
    """Search terms: identifiers split into words, stop words dropped, plurals folded."""
    return [stem(w) for w in words(text or "") if w not in _STOP]


def first_line(text: str | None, limit: int | None = 120) -> str:
    """The text on one line, cut to `limit` characters (None: not cut)."""
    if not text:
        return ""
    line = " ".join(str(text).split())
    return line if limit is None or len(line) <= limit else line[:limit - 1].rstrip() + "…"


def full_text(text) -> str:
    """A description as written, for to_dict(): never cut."""
    return str(text).strip() if text else ""


def label_key(label: str):
    """Sort key for labels: "2.10" after "2.2", runs of digits compared as numbers."""
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.split(r"(\d+)", label) if part]


# A path as a question or an answer writes it, after its method or alone: the characters a path segment may hold
# (RFC 3986: letters, digits, - . _ ~ and the sub-delimiters, : @), braces for templates, and the # and = some
# converted specs put in their paths.
PATH = r"/[\w\-{}.~/:@!$&'()*+,;=#]*"


def trim_path(path: str) -> str:
    """A path without what the sentence put after it: "(see /pets)." -> /pets, "/certificates(id={id})" kept."""
    while path:
        if path[-1] in ".,;:?!'":
            path = path[:-1]
        elif path[-1] == ")" and path.count(")") > path.count("("):
            path = path[:-1]
        else:
            break
    return path
