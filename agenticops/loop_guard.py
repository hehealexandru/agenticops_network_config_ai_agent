from __future__ import annotations

import json
from typing import Any


def _call_key(name: str, arguments: Any) -> str:
    try:
        args = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        args = repr(arguments)
    return f"{name}:{args}"


class LoopGuard:
    def __init__(self, max_identical: int = 2, max_blocked: int = 2, max_errors: int = 3) -> None:
        self.max_identical = max_identical
        self.max_blocked = max_blocked
        self.max_errors = max_errors
        self._last_key: str | None = None
        self._identical = 0
        self._blocked = 0
        self._error_tool: str | None = None
        self._errors = 0
        self.stop_reason: str | None = None

    def check(self, name: str, arguments: Any) -> str | None:
        """Call before running a tool. Returns an error message if the call is blocked."""
        key = _call_key(name, arguments)
        self._identical = self._identical + 1 if key == self._last_key else 1
        self._last_key = key
        if self._identical <= self.max_identical:
            return None
        self._blocked += 1
        if self._blocked >= self.max_blocked:
            self.stop_reason = (f"the model kept repeating the same {name} call "
                                "with identical arguments")
        return (f"Blocked: {name} was already called {self.max_identical} times in a row with "
                "these exact arguments and the result will not change. This is not a rate "
                "limit. Use the previous results, change the approach, or answer the user.")

    def record(self, name: str, ok: bool) -> None:
        """Call after a tool ran (or was blocked) with whether it succeeded."""
        if ok:
            self._error_tool, self._errors = None, 0
            return
        self._errors = self._errors + 1 if name == self._error_tool else 1
        self._error_tool = name
        if self._errors >= self.max_errors and not self.stop_reason:
            self.stop_reason = f"{name} failed {self._errors} times in a row"

    @property
    def should_stop(self) -> bool:
        return self.stop_reason is not None