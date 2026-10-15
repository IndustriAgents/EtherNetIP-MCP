from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class TagMap:
    """Aliases loaded from ``TAG_MAP_FILE``; reloaded when the file changes."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.error: str | None = None
        self._tags: dict[str, dict[str, Any]] = {}
        self._stamp: tuple[int, int] | None = None
        self.refresh()

    def refresh(self) -> None:
        if not self.path:
            self._tags, self._stamp, self.error = ({}, None, None)
            return
        try:
            stat = self.path.stat()
        except OSError as exc:
            self._tags, self._stamp = ({}, None)
            self.error = f"TAG_MAP_FILE {self.path} cannot be read: {exc.strerror or exc}"
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._tags = {}
            self.error = f"TAG_MAP_FILE {self.path} is not valid JSON: {exc}"
            return
        if not isinstance(data, dict) or not all(isinstance(spec, dict) for spec in data.values()):
            self._tags = {}
            self.error = f"TAG_MAP_FILE {self.path} must be a JSON object mapping each alias to an object"
            return
        self._tags = {str(alias): spec for alias, spec in data.items()}
        self.error = None

    def get(self, alias: str) -> dict[str, Any] | None:
        self.refresh()
        return self._tags.get(alias)

    def list(self) -> list[dict[str, Any]]:
        self.refresh()
        return [
            {
                "alias": alias,
                "tag": spec.get("tag"),
                "data_type": spec.get("data_type"),
                "description": spec.get("description"),
                "scaling": spec.get("scaling"),
            }
            for alias, spec in self._tags.items()
        ]

    def count(self) -> int:
        self.refresh()
        return len(self._tags)
