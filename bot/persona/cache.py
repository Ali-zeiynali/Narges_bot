from dataclasses import dataclass


@dataclass
class CachedPersona:
    version: str
    sections_key: str
    prompt: str


class PersonaCache:
    def __init__(self) -> None:
        self._version: str | None = None
        self._items: dict[str, CachedPersona] = {}

    def get(self, version: str, sections_key: str) -> str | None:
        if self._version != version:
            self.clear(version)
            return None
        item = self._items.get(sections_key)
        return item.prompt if item else None

    def set(self, version: str, sections_key: str, prompt: str) -> None:
        if self._version != version:
            self.clear(version)
        self._items[sections_key] = CachedPersona(version, sections_key, prompt)

    def clear(self, version: str | None = None) -> None:
        self._items.clear()
        self._version = version
