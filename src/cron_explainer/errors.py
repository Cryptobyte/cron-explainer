"""Errors that teach.

A :class:`CronError` always knows *which* field failed, *where* in the original
string the problem is, what was expected there, and (when it is obvious) what to
write instead. Nothing in this package ever lets a traceback escape to a caller.
"""

from __future__ import annotations

from typing import Any


class CronError(ValueError):
    """A problem with a cron expression, described well enough to act on.

    Parameters
    ----------
    message:
        One sentence saying what is wrong, in plain English.
    field:
        Human label of the offending field, e.g. ``"day-of-week"``.
    position:
        Zero-based character offset into ``expression`` where the problem starts.
    expected:
        What the parser was willing to accept at that position.
    suggestion:
        A corrected expression or fragment, when one is obvious.
    expression:
        The original expression, so the error can draw a caret under it.
    """

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        position: int | None = None,
        expected: str | None = None,
        suggestion: str | None = None,
        expression: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.position = position
        self.expected = expected
        self.suggestion = suggestion
        self.expression = expression

    def with_expression(self, expression: str) -> CronError:
        """Return a copy that knows the full expression (for the caret line)."""
        if self.expression is not None:
            return self
        return CronError(
            self.message,
            field=self.field,
            position=self.position,
            expected=self.expected,
            suggestion=self.suggestion,
            expression=expression,
        )

    def caret_line(self) -> str | None:
        """Render the expression with a caret pointing at the problem."""
        if self.expression is None or self.position is None:
            return None
        if not 0 <= self.position <= len(self.expression):
            return None
        return f"{self.expression}\n{' ' * self.position}^"

    def format(self) -> str:
        """A complete, multi-line, human-readable report."""
        head = self.message
        if self.field and self.field not in head:
            head = f"{head} (field: {self.field})"
        parts = [head]
        caret = self.caret_line()
        if caret:
            parts.append(caret)
        if self.expected:
            parts.append(f"Expected: {self.expected}")
        if self.suggestion:
            parts.append(f"Suggestion: {self.suggestion}")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Structured form, for the JSON half of a tool response."""
        return {
            "message": self.message,
            "field": self.field,
            "position": self.position,
            "expected": self.expected,
            "suggestion": self.suggestion,
            "expression": self.expression,
            "pointer": self.caret_line(),
        }

    def __str__(self) -> str:
        return self.format()
