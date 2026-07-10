from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True)
class CachedPersona:
    version: str
    sections_key: str
    prompt: str


class PersonaCache:
    def __init__(self, max_items: int = 16) -> None:
        self.max_items = max(1, int(max_items))
        self._items: OrderedDict[tuple[str, str], CachedPersona] = OrderedDict()
        self._lock = RLock()

    def get(self, version: str, sections_key: str) -> str | None:
        key = (version, sections_key)
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            self._items.move_to_end(key)
            return item.prompt

    def set(self, version: str, sections_key: str, prompt: str) -> None:
        key = (version, sections_key)
        with self._lock:
            self._items[key] = CachedPersona(version, sections_key, prompt)
            self._items.move_to_end(key)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)

    def clear(self, version: str | None = None) -> None:
        with self._lock:
            if version is None:
                self._items.clear()
                return
            stale = [key for key in self._items if key[0] == version]
            for key in stale:
                self._items.pop(key, None)
