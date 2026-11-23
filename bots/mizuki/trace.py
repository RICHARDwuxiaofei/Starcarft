"""Bounded optional decision diagnostics. Nothing is written during a match."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DecisionTrace:
    sample_interval_s: float = 12
    sample_limit: int = 300
    decision_limit: int = 400
    samples: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    start_completed: bool = False
    steps_completed: int = 0
    last_error: dict[str, Any] | None = None
    dropped_samples: int = 0
    dropped_decisions: int = 0
    _last_sample: float = -999
    _phase: str | None = None

    def sample(self, time: float, phase: str, **details: Any) -> None:
        changed = phase != self._phase
        if not changed and time - self._last_sample < self.sample_interval_s:
            return
        self._last_sample = time
        if changed:
            self.record(time, "combat_phase", previous=self._phase, current=phase)
            self._phase = phase
        if len(self.samples) >= self.sample_limit:
            self.dropped_samples += 1
            return
        self.samples.append({"t": round(time, 2), "phase": phase, **details})

    def record(self, time: float, event: str, **details: Any) -> None:
        if len(self.decisions) >= self.decision_limit:
            self.dropped_decisions += 1
            return
        self.decisions.append({"t": round(time, 2), "event": event, **details})

    def export(self) -> dict[str, Any]:
        return {
            "start_completed": self.start_completed,
            "steps_completed": self.steps_completed,
            "last_error": self.last_error,
            "sample_interval_seconds": self.sample_interval_s,
            "samples": list(self.samples),
            "decisions": list(self.decisions),
            "dropped_samples": self.dropped_samples,
            "dropped_decisions": self.dropped_decisions,
            "scope": "Self-observations and locally queued intents. Use replay events to confirm execution.",
        }
