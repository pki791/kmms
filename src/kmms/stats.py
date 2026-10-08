"""Success / error counters shown per set and in the status bar."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Counters:
    """Counts successful and failed reads plus the most recent outcome."""

    ok: int = 0
    errors: int = 0
    timeouts: int = 0
    last_ms: float = 0.0
    last_error: str = ""

    def record(self, ok: bool, duration_ms: float = 0.0, error: str = "",
               error_kind: str = "") -> None:
        """Register the outcome of one poll."""
        if ok:
            self.ok += 1
            self.last_ms = duration_ms
            self.last_error = ""
        else:
            self.errors += 1
            if error_kind == "timeout":
                self.timeouts += 1
            self.last_error = error

    def reset(self) -> None:
        """Set all counters back to zero."""
        self.ok = 0
        self.errors = 0
        self.timeouts = 0
        self.last_ms = 0.0
        self.last_error = ""

    @property
    def total(self) -> int:
        """Return the number of polls recorded."""
        return self.ok + self.errors
