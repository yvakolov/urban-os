#!/usr/bin/env python3
"""Состояние пересмотра профилей относительно их источников.

Отвечает на один вопрос: перечитал ли человек этот профиль после того, как
источник под ним сдвинулся. Ответ «нет» блокирует выпуск.

Почему отдельный файл, а не поле в профиле. Опубликованный профиль неизменяем,
а это состояние меняется при каждом сдвиге источника. Хранить его внутри значило
бы либо править опубликованное, либо выпускать новую редакцию требований там,
где сами требования не менялись.

    python3 tools/review.py --status
    python3 tools/review.py --approve agr-request-package --source msk-284-pp \\
        --by "Фамилия И.О." --verdict requirements_unchanged --note "..."

Утверждение не ходит в сеть: берётся хеш из утверждённого baseline снапшота.
Иначе человек подтверждал бы редакцию, которой никто не видел.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "review" / "state.json"
SOURCES = ROOT / "sources" / "index.json"
REQ = ROOT / "requirements"
SNAP = ROOT / "sources" / "snapshots"

CURRENT = "current"
STALE = "STALE"
NEVER = "NEVER_REVIEWED"
UNMONITORED = "unmonitored"


def load():
    state = json.loads(STATE.read_text(encoding="utf-8"))
    sources = json.loads(SOURCES.read_text(encoding="utf-8"))["sources"]
    profiles = {}
    for path in sorted(REQ.glob("*.json")):
        p = json.loads(path.read_text(encoding="utf-8"))
        if p.get("status") == "active":
            profiles[p["id"]] = p
    return state, sources, profiles


def closure(source_id, sources):
    """Источник и все его предки по partOf: сдвиг предка задевает потомка."""
    chain, cur = [], source_id
    while cur and cur not in chain:
        chain.append(cur)
        cur = sources.get(cur, {}).get("partOf")
    return chain


def baseline_hash(source_id):
    f = SNAP / source_id / "baseline.json"
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8")).get("normalizedHash")


def key(profile):
    return f"{profile['id']}@{profile['version']}"


def status(state, sources, profiles):
    rows = []
    for pid, p in sorted(profiles.items()):
        reviewed = (state["reviews"].get(key(p)) or {}).get("sources", {})
        for sid in closure(p["sourceId"], sources):
            current = baseline_hash(sid)
            if current is None:
                # Источник не под наблюдением — сдвиг его текста мы не заметим,
                # и говорить о свежести пересмотра нечего.
                rows.append((pid, sid, UNMONITORED, None, None))
                continue
            seen = (reviewed.get(sid) or {}).get("normalizedHash")
            if seen is None:
                rows.append((pid, sid, NEVER, current, None))
            elif seen != current:
                rows.append((pid, sid, STALE, current, seen))
            else:
                rows.append((pid, sid, CURRENT, current, seen))
    return rows


def blocked_profiles(rows):
    """Профили, выпуск по которым заблокирован непересмотренным источником."""
    return sorted({pid for pid, _sid, st, _c, _s in rows if st in (STALE, NEVER)})


def approve(args):
    state, sources, profiles = load()
    p = profiles.get(args.approve)
    if not p:
        print(f"{args.approve}: активного профиля с таким id нет")
        return 1
    sid = args.source or p["sourceId"]
    h = baseline_hash(sid)
    if h is None:
        print(f"{sid}: утверждённого baseline нет — нечего подтверждать. "
              f"Сначала tools/check_sources.py --approve")
        return 1
    entry = state["reviews"].setdefault(key(p), {"sources": {}})
    entry["sources"][sid] = {
        "normalizedHash": h,
        "reviewedOn": args.on,
        "reviewedBy": args.by,
        "verdict": args.verdict,
        "note": args.note or "",
    }
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{key(p)} ← {sid} {h[:12]} · {args.verdict} · {args.by}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--approve", metavar="PROFILE")
    ap.add_argument("--source")
    ap.add_argument("--by", default="")
    ap.add_argument("--on", default="")
    ap.add_argument("--verdict", default="requirements_unchanged",
                    choices=["requirements_unchanged", "revised", "initial"])
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    if args.approve:
        if not args.by or not args.on:
            print("--by и --on обязательны: пересмотр — действие человека, "
                  "и в записи должно быть видно, кто и когда его сделал")
            return 1
        return approve(args)

    state, sources, profiles = load()
    rows = status(state, sources, profiles)
    width = max(len(r[0]) for r in rows) if rows else 10
    for pid, sid, st, cur, seen in rows:
        extra = ""
        if st == STALE:
            extra = f"  baseline {cur[:12]} ≠ пересмотрено {seen[:12]}"
        elif st == NEVER:
            extra = f"  baseline {cur[:12]}, пересмотра не было"
        print(f"{pid:{width}}  {sid:22} {st:16}{extra}")
    blocked = blocked_profiles(rows)
    print()
    if blocked:
        print("Выпуск заблокирован до пересмотра человеком:", ", ".join(blocked))
        return 2
    print("Все наблюдаемые источники пересмотрены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
