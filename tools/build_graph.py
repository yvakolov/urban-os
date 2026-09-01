#!/usr/bin/env python3
"""Сборка графа «источник → требование → артефакт» для визуализации.

    python3 tools/build_graph.py            собрать
    python3 tools/build_graph.py --check    проверить, что собранное совпадает

Выход: apps/frontend/client/src/assets/requirements-graph.json

Граф отвечает на вопрос, который иначе приходится держать в голове: если
изменилась вот эта часть источника — какие требования пересматривать и какие
файлы пересобирать. Слои и рёбра берутся из уже существующих реестров, ничего
не выдумывается:

  источник   ветки window.DATA + нормативные акты (sources/index.json)
     ↓       monitoring/sources.json → impactMap
  требование правила профилей (requirements/*.json)
     ↓       deliverables/index.json → producedBy
  артефакт   файл, пакет, внешний документ или заключение

Без зависимостей.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "apps" / "frontend" / "client" / "src" / "assets" / "requirements-graph.json"

SEVERITY_ORDER = {"minor": 1, "major": 2, "blocker": 3}


def load():
    sources = json.loads((ROOT / "sources" / "index.json").read_text(encoding="utf-8"))
    monitors = json.loads((ROOT / "monitoring" / "sources.json").read_text(encoding="utf-8"))["monitors"]
    delivs = json.loads((ROOT / "deliverables" / "index.json").read_text(encoding="utf-8"))["deliverables"]
    profiles = {}
    for p in sorted((ROOT / "requirements").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        # Перекрытая редакция в граф не идёт: показывать надо действующие
        # требования. Полагаться на порядок имён файлов нельзя — v10 встал бы
        # перед v2, и витрина показала бы устаревшую редакцию как текущую.
        if d.get("status") == "superseded":
            continue
        profiles[d["id"]] = d
    return sources, monitors, delivs, profiles


def build():
    sources, monitors, delivs, profiles = load()
    nodes, links = [], []
    seen = set()

    def node(nid, **kw):
        if nid in seen:
            return
        seen.add(nid)
        nodes.append({"id": nid, **kw})

    def link(a, b, kind):
        links.append({"source": a, "target": b, "kind": kind})

    # ── слой 1: источники и их наблюдаемые ветки ─────────────────────
    for sid, s in sources["sources"].items():
        transcribed = s.get("transcribed", "none")
        node(
            f"src:{sid}", layer="source", label=s["authority"][:90],
            status=s.get("status"), transcribed=transcribed,
            monitored=sid in monitors, url=s.get("url"),
        )
        if s.get("partOf"):
            link(f"src:{s['partOf']}", f"src:{sid}", "partOf")

    for sid, mon in monitors.items():
        for branch, mapped in (mon.get("impactMap") or {}).items():
            bid = f"branch:{sid}:{branch}"
            is_registry = mapped == "__source_registry__"
            node(
                bid, layer="branch", label=branch, sourceId=sid,
                registry=is_registry,
                ruleCount=0 if is_registry else len(mapped),
            )
            link(f"src:{sid}", bid, "branch")

    # ── слой 2: требования ───────────────────────────────────────────
    rule_index = {}
    for pid, p in profiles.items():
        node(
            f"profile:{pid}", layer="profile", label=pid,
            status=p["status"], sourceId=p["sourceId"],
            ruleCount=len(p["rules"]),
        )
        link(f"src:{p['sourceId']}", f"profile:{pid}", "transcribedInto")
        for r in p["rules"]:
            rid = f"rule:{r['id']}"
            rule_index[r["id"]] = rid
            node(
                rid, layer="rule", label=r["title"], ruleId=r["id"],
                profileId=pid, category=r["category"],
                severity=r["severity"], verification=r["verification"],
                pending=r.get("status") == "pending_activation",
                sourceRef=r["sourceRef"],
            )
            link(f"profile:{pid}", rid, "contains")
            if r.get("dependsOnSource"):
                link(f"src:{r['dependsOnSource']}", rid, "dependsOnSource")

    # ветка источника -> конкретные правила (это и есть «квант» изменения)
    for sid, mon in monitors.items():
        for branch, mapped in (mon.get("impactMap") or {}).items():
            if mapped == "__source_registry__":
                continue
            for rid in mapped:
                if rid in rule_index:
                    link(f"branch:{sid}:{branch}", rule_index[rid], "impacts")

    # ── слой 3: артефакты ────────────────────────────────────────────
    for did, d in delivs.items():
        node(
            f"deliv:{did}", layer="deliverable", label=d["label"],
            kind=d["kind"], origin=d.get("origin"), fmt=d.get("format"),
            blockedBy=d.get("blockedBy"), isOutcome=d.get("isOutcome", False),
        )
        for rid in d.get("producedBy", []) or []:
            if rid in rule_index:
                link(rule_index[rid], f"deliv:{did}", "produces")
        for child in d.get("contains", []) or []:
            link(f"deliv:{did}", f"deliv:{child}", "contains")
        # пакет модели порождается всем профилем целиком
        if d.get("profile") and f"profile:{d['profile']}" in seen:
            link(f"profile:{d['profile']}", f"deliv:{did}", "produces")

    # всё сходится к результату услуги
    for did, d in delivs.items():
        if d.get("isOutcome") or d.get("containedIn"):
            continue
        link(f"deliv:{did}", "deliv:agr-certificate", "submitted")

    counts = {}
    for n in nodes:
        counts[n["layer"]] = counts.get(n["layer"], 0) + 1

    return {
        "generatedFrom": "sources/index.json · monitoring/sources.json · requirements/*.json · deliverables/index.json",
        "note": "Сгенерировано tools/build_graph.py. Руками не править — правьте реестры.",
        "counts": counts,
        "nodes": nodes,
        "links": links,
    }


def main():
    graph = build()
    payload = json.dumps(graph, ensure_ascii=False, indent=2) + "\n"

    if "--check" in sys.argv:
        if not OUT.exists():
            print(f"нет {OUT.relative_to(ROOT)} — запустите без --check")
            return 1
        if OUT.read_text(encoding="utf-8") != payload:
            print("граф разошёлся с реестрами — пересоберите: python3 tools/build_graph.py")
            return 1
        print("граф актуален")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(payload, encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)}")
    print("  узлов: " + " · ".join(f"{k} {v}" for k, v in sorted(graph["counts"].items())))
    print(f"  рёбер: {len(graph['links'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
