#!/usr/bin/env python3
"""Одноразовый перенос профилей schemaVersion 1.0 -> 1.1.

Что делает: вытаскивает провенанс из профиля в sources/index.json (он уже там),
заменяет его ссылкой sourceId, добавляет status / supersedes / selfHash.
Правила не трогает — только их обёртку.

Запускать из корня репозитория:
    python3 tools/migrate_v1.py <каталог-со-старыми-профилями>
"""
import json
import sys
from pathlib import Path

from validate import self_hash  # общий расчёт хэша, чтобы не разошлись

ROOT = Path(__file__).resolve().parent.parent

# старый id профиля -> (sourceId, status, supersedes)
BINDING = {
    "moscow-npm-2026-08-18": ("msk-3d-2026-08-18", "active", "moscow-npm-2026-01-19"),
    "moscow-vpm-2026-08-18": ("msk-3d-2026-08-18", "active", "moscow-vpm-2026-01-19"),
    "moscow-oblast-npm-2025-12": ("mo-npm-2025-12", "active", None),
    "advertising-structures-2025-07": ("msk-advert-2025-07", "active", None),
}


def migrate(old: dict) -> dict:
    source_id, status, supersedes = BINDING[old["id"]]
    new = {
        "schemaVersion": "1.1",
        "id": old["id"],
        "version": old["version"],
        "status": status,
        "sourceId": source_id,
        "jurisdiction": old["jurisdiction"],
        "modelClass": old["modelClass"],
        "effectiveFrom": old["effectiveFrom"],
        "releasePolicy": old["releasePolicy"],
        "rules": old["rules"],
    }
    if supersedes:
        new["supersedes"] = supersedes
    new["selfHash"] = self_hash(new)
    return new


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    src_dir = Path(sys.argv[1])
    out_dir = ROOT / "requirements"
    out_dir.mkdir(exist_ok=True)

    for path in sorted(src_dir.glob("*.v1.json")):
        old = json.loads(path.read_text(encoding="utf-8"))
        if old["id"] not in BINDING:
            print(f"пропуск (нет привязки): {path.name}")
            continue
        new = migrate(old)
        out = out_dir / path.name
        out.write_text(
            json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"{path.name}: {len(new['rules'])} правил -> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
