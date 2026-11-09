"""Problems found while loading a spec, each with the place it came from."""

from dataclasses import dataclass

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    severity: str  # ERROR or WARNING
    code: str  # stable, machine-readable: "unresolved-ref", "parse-error", ...
    message: str
    file: str | None = None
    line: int | None = None

    def where(self) -> str:
        if self.file is None:
            return ""
        return f"{self.file}:{self.line}" if self.line else self.file

    def to_text(self) -> str:
        where = self.where()
        return f"{self.severity} [{self.code}] {where + ': ' if where else ''}{self.message}"

    def to_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code, "message": self.message,
                "file": self.file, "line": self.line}
