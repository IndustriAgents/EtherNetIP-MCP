"""Bounded retained metadata snapshots, independent of native controller sessions."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

MAX_SNAPSHOT_TAGS = 20_000
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_PAGE_BYTES = 128 * 1024
MAX_PAGE_TAGS = 500
SNAPSHOT_TTL = 60.0


@dataclass(frozen=True)
class TagSnapshot:
    token: str
    created: float
    program: str | None
    prefix: str
    rows: tuple[str, ...]
    sizes: tuple[int, ...]
    uploaded_count: int

    @classmethod
    def capture(cls, tags: list[dict[str, Any]], program: str | None, prefix: str) -> TagSnapshot:
        rows, sizes = [], []
        total = 0
        for tag in tags:
            if not isinstance(tag, dict) or not isinstance(tag.get("tag"), str) or not tag["tag"]:
                raise ValueError("Controller returned malformed tag metadata; no complete snapshot is available")
            if not tag["tag"].startswith(prefix):
                continue
            if len(rows) >= MAX_SNAPSHOT_TAGS:
                raise ValueError("Tag snapshot exceeds 20000 entries; narrow program or prefix")
            row = json.dumps(tag, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
            # Budget for the final pretty-spaced JSON form and its list separator.
            size = len(json.dumps(tag, ensure_ascii=True, allow_nan=False).encode()) + 2
            if size > MAX_PAGE_BYTES:
                raise ValueError("One tag definition exceeds the page byte limit; narrow the prefix")
            total += size
            if total > MAX_SNAPSHOT_BYTES:
                raise ValueError("Tag snapshot exceeds 4 MiB; narrow program or prefix")
            rows.append(row)
            sizes.append(size)
        return cls(uuid.uuid4().hex, time.monotonic(), program, prefix, tuple(rows), tuple(sizes), len(tags))

    def page(self, program: str | None, prefix: str, limit: int, cursor: str | None) -> tuple[dict, dict]:
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_TAGS:
            raise ValueError("limit must be an integer in 1..500")
        age = time.monotonic() - self.created
        if age >= SNAPSHOT_TTL:
            raise ValueError("Tag cursor expired; start a new browse without cursor")
        if (program, prefix) != (self.program, self.prefix):
            raise ValueError("Tag cursor belongs to another program or prefix")
        offset = 0
        if cursor is not None:
            parts = cursor.split(":")
            if len(parts) != 2 or parts[0] != self.token or not parts[1].isascii() or not parts[1].isdigit():
                raise ValueError("Unknown tag cursor; start a new browse without cursor")
            offset = int(parts[1])
            if not 0 < offset < len(self.rows):
                raise ValueError("Tag cursor offset is outside the snapshot")
        end, byte_count = offset, 0
        while end < len(self.rows) and end - offset < limit:
            size = self.sizes[end]
            if byte_count + size > MAX_PAGE_BYTES:
                break
            byte_count += size
            end += 1
        return (
            {"program": program, "tags": [json.loads(row) for row in self.rows[offset:end]]},
            {
                "count": end - offset,
                "total_count": len(self.rows),
                "uploaded_count": self.uploaded_count,
                "offset": offset,
                "complete": end == len(self.rows),
                "next_cursor": f"{self.token}:{end}" if end < len(self.rows) else None,
                "snapshot_id": self.token,
                "snapshot_age_ms": round(age * 1000, 3),
                "snapshot_ttl_seconds": SNAPSHOT_TTL,
                "source": "metadata_snapshot",
                "prefix": prefix,
                "page_bytes": byte_count,
            },
        )
