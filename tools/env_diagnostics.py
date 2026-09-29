"""Machine-readable diagnostics for Types, Catalogs and Recipes.

Every problem is reported as
    {"severity", "path", "code", "expected", "actual", "reason"}
so a person reads the reason and a program (or an AI repairing a Recipe)
reads the path and code. A check collects all the problems it can find in
one pass instead of stopping at the first (docs/data-contract.md, "Diagnostics").
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Codes used across the checks (a stable vocabulary for tools and AI).
CODES = {
    "unknown_field": "a field that the schema does not define (often a typo)",
    "missing_field": "a required field is absent",
    "wrong_type": "the value has the wrong kind (text for a number, ...)",
    "out_of_range": "a number outside its allowed range",
    "not_one_of": "a value that is not one of the allowed choices",
    "unknown_reference": "a type, item or parameter that does not exist",
    "duplicate_id": "an id used twice",
    "not_allowed": "a value this level may not set (fixed by the type or the item)",
    "invalid_expression": "an expression that cannot be read or evaluated",
    "invalid_shape": "a type that makes a shape with no size, or a bad primitive",
    "wrong_schema": "the schema tag is missing or of another version",
    "overlap": "two objects penetrate each other",
    "outside": "an object reaches outside the environment",
    "below_terrain": "an object reaches into the terrain",
    "compile_error": "MuJoCo could not compile the generated world",
}


@dataclass(frozen=True)
class Diagnostic:
    path: str
    code: str
    reason: str
    expected: object = None
    actual: object = None
    severity: str = "error"

    def as_json(self) -> dict:
        return asdict(self)

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


class DiagnosticError(ValueError):
    """One or more problems; str() gives the first, .diagnostics all of them."""

    def __init__(self, diagnostics: list[Diagnostic]):
        self.diagnostics = list(diagnostics)
        super().__init__("; ".join(str(item) for item in self.diagnostics[:3])
                         + (f" (and {len(self.diagnostics) - 3} more)" if len(self.diagnostics) > 3 else ""))


def fail(path: str, code: str, reason: str, expected=None, actual=None) -> DiagnosticError:
    return DiagnosticError([Diagnostic(path, code, reason, expected, actual)])


class Collector:
    """Gathers diagnostics; `check` runs a step and keeps going when it fails."""

    def __init__(self):
        self.items: list[Diagnostic] = []

    def add(self, path: str, code: str, reason: str, expected=None, actual=None, severity: str = "error") -> None:
        self.items.append(Diagnostic(path, code, reason, expected, actual, severity))

    def check(self, step, *args, **kwargs):
        """step(...) or None when it raised DiagnosticError (whose problems are kept)."""
        try:
            return step(*args, **kwargs)
        except DiagnosticError as error:
            self.items.extend(error.diagnostics)
            return None

    @property
    def errors(self) -> list[Diagnostic]:
        return [item for item in self.items if item.severity == "error"]

    def raise_if_errors(self) -> None:
        if self.errors:
            raise DiagnosticError(self.errors)


def unknown_fields(value: dict, allowed: set[str], path: str) -> list[Diagnostic]:
    return [Diagnostic(f"{path}.{key}" if path else key, "unknown_field", f"{key!r} is not a field here",
                       expected=sorted(allowed), actual=key)
            for key in sorted(set(value) - allowed)]


def only(value: dict, allowed: set[str], path: str) -> None:
    problems = unknown_fields(value, allowed, path)
    if problems:
        raise DiagnosticError(problems)


def mapping(value, path: str) -> dict:
    if not isinstance(value, dict):
        raise fail(path, "wrong_type", "must be a mapping", expected="mapping", actual=type(value).__name__)
    return value
