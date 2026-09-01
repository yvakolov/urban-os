#!/usr/bin/env python3
"""Дозагрузка документа-источника с проверкой sha256 по реестру.

Крупные источники в репозитории не хранятся: распоряжение о требованиях к IFC
весит 17 МБ сканов. Но провенанс от этого страдать не должен, поэтому реестр
объявляет и адрес, и хеш, а этот инструмент скачивает файл и сверяет.

    python3 tools/fetch_source.py msk-ifc-agr
    python3 tools/fetch_source.py --all

Несовпадение хеша — не повод обновить реестр. Это значит, что по адресу лежит
другой документ, и решение принимает человек.
"""

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources" / "index.json"
FILES = ROOT / "sources" / "files"
USER_AGENT = "urban-os-source-fetch/1.0"
TIMEOUT = 180


def fetch(source_id, spec):
    url = spec.get("url")
    if not url:
        return f"{source_id}: адреса нет — файл получить неоткуда"
    declared = spec.get("sha256")
    target = ROOT / (spec.get("file") or f"sources/files/{source_id}.pdf")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    actual = hashlib.sha256(raw).hexdigest()
    if declared and actual != declared:
        return (f"{source_id}: ХЕШ НЕ СОВПАЛ\n"
                f"  объявлен: {declared}\n  получен:  {actual}\n"
                f"  По адресу лежит другой документ. Реестр не трогать — это решение человека.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    verdict = "хеш сошёлся" if declared else "хеш не был объявлен, записан впервые"
    return f"{source_id}: {len(raw)} байт -> {target.relative_to(ROOT)} ({verdict}) {actual}"


def main():
    doc = json.loads(SOURCES.read_text(encoding="utf-8"))["sources"]
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--all" in sys.argv:
        args = [k for k, v in doc.items() if v.get("url", "").endswith(".pdf")]
    if not args:
        print(__doc__)
        print("Доступные источники с адресом:")
        for k, v in doc.items():
            if v.get("url"):
                print(f"  {k}")
        return 1
    rc = 0
    for sid in args:
        if sid not in doc:
            print(f"{sid}: нет в реестре")
            rc = 1
            continue
        try:
            line = fetch(sid, doc[sid])
        except Exception as exc:
            line = f"{sid}: не скачался — {type(exc).__name__}: {exc}"
        print(line)
        if "НЕ СОВПАЛ" in line or "не скачался" in line:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
