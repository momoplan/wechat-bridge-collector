from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Cursor:
    create_time: int = 0
    local_id: int = 0

    @classmethod
    def from_json(cls, value: dict[str, Any] | None) -> "Cursor":
        if not value:
            return cls()
        return cls(
            create_time=int(value.get("create_time") or 0),
            local_id=int(value.get("local_id") or 0),
        )

    def to_json(self) -> dict[str, int]:
        return {"create_time": self.create_time, "local_id": self.local_id}


@dataclass
class CollectorState:
    schema_version: int = 1
    sessions: dict[str, int] = field(default_factory=dict)
    cursors: dict[str, Cursor] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "CollectorState":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            schema_version=int(raw.get("schema_version") or 1),
            sessions={str(k): int(v or 0) for k, v in raw.get("sessions", {}).items()},
            cursors={
                str(k): Cursor.from_json(v)
                for k, v in raw.get("cursors", {}).items()
            },
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "schema_version": self.schema_version,
            "sessions": self.sessions,
            "cursors": {k: v.to_json() for k, v in self.cursors.items()},
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def cursor_for(self, key: str) -> Cursor | None:
        return self.cursors.get(key)

    def set_cursor(self, key: str, create_time: int, local_id: int) -> None:
        current = self.cursors.get(key)
        if current and (create_time, local_id) < (current.create_time, current.local_id):
            return
        self.cursors[key] = Cursor(create_time=create_time, local_id=local_id)

