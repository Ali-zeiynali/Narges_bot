from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Iterator


@dataclass
class RequestTrace:
    name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=perf_counter)
    steps: list[dict[str, Any]] = field(default_factory=list)

    @contextmanager
    def step(self, name: str, **metadata: Any) -> Iterator[None]:
        started = perf_counter()
        try:
            yield
        finally:
            self.add(name, int((perf_counter() - started) * 1000), **metadata)

    def add(self, name: str, elapsed_ms: int, **metadata: Any) -> None:
        payload = {"name": name, "elapsed_ms": int(elapsed_ms)}
        payload.update({key: value for key, value in metadata.items() if value is not None})
        self.steps.append(payload)

    def finish(self, **metadata: Any) -> dict[str, Any]:
        total_ms = int((perf_counter() - self.started_at) * 1000)
        merged = dict(self.metadata)
        merged.update({key: value for key, value in metadata.items() if value is not None})
        return {
            "name": self.name,
            "total_ms": total_ms,
            "metadata": merged,
            "steps": self.steps,
        }
