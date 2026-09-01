#!/usr/bin/env python3
"""Проверка целостности реестра требований и прогон золотых фикстур.

    python3 tools/validate.py            проверить
    python3 tools/validate.py --rehash   пересчитать selfHash (только для draft)

Без зависимостей. Ненулевой код возврата = что-то сломано.
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ_DIR = ROOT / "requirements"
FIXTURE_DIR = ROOT / "tests" / "fixtures"

VERIFICATION = {"automatic", "expert_evidence", "external_fact", "external_verdict"}
SEVERITY = {"blocker", "major", "minor"}
RULE_STATUS = {"active", "pending_activation"}
OPS = {"max", "min", "range", "equals", "regex", "all_zero"}
REQUIRED_RULE_FIELDS = ("id", "category", "title", "requirement", "verification", "severity", "sourceRef")


def self_hash(profile: dict) -> str:
    """sha256 канонического JSON профиля без самого поля selfHash."""
    body = {k: v for k, v in profile.items() if k != "selfHash"}
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- evaluator

def get_path(ctx, path):
    cur = ctx
    for key in str(path or "").split("."):
        if cur is None:
            return None
        cur = cur.get(key) if isinstance(cur, dict) else None
    return cur


def is_missing(v):
    return v is None or v == ""


def evaluate(profile: dict, context: dict, decisions: dict | None = None) -> dict:
    """Портированный evaluateProfile. Порядок ветвлений повторяет core.js."""
    decisions = decisions or {}
    counts = dict.fromkeys(
        ("auto_pass", "auto_fail", "expert_reviewed", "not_applicable", "pending", "awaiting_external"), 0
    )
    counts["total"] = 0

    for rule in profile["rules"]:
        counts["total"] += 1
        state = _state_for(rule, context, decisions.get(rule["id"]))
        counts[state] = counts.get(state, 0) + 1

    counts["terminal"] = counts["auto_pass"] + counts["expert_reviewed"] + counts["not_applicable"]
    counts["releaseEligible"] = (
        counts["total"] > 0
        and counts["pending"] == 0
        and counts["auto_fail"] == 0
        and counts["awaiting_external"] == 0
    )
    return counts


def _state_for(rule, context, decision):
    if rule.get("status") == "pending_activation":
        return "not_applicable"

    cond = rule.get("appliesWhen")
    if cond:
        value = get_path(context, cond["path"])
        if is_missing(value):
            return "pending"
        if "equals" in cond and value != cond["equals"]:
            return "not_applicable"
        if "in" in cond and value not in cond["in"]:
            return "not_applicable"

    if decision and decision.get("state") in ("expert_reviewed", "not_applicable"):
        return decision["state"]

    if rule["verification"] in ("external_fact", "external_verdict"):
        return "awaiting_external"

    if rule["verification"] == "automatic":
        return _auto(rule, context)

    return "pending"


def _auto(rule, context):
    ev = rule.get("evaluator") or {}
    op = ev.get("op")

    if op == "all_zero":
        vals = [get_path(context, p) for p in ev.get("paths", [])]
        if any(is_missing(v) for v in vals):
            return "pending"
        return "auto_pass" if all(float(v) == 0 for v in vals) else "auto_fail"

    value = get_path(context, ev.get("path"))
    if is_missing(value):
        return "pending"

    try:
        if op == "max":
            ok = float(value) <= float(ev["value"])
        elif op == "min":
            ok = float(value) >= float(ev["value"])
        elif op == "range":
            ok = float(ev["min"]) <= float(value) <= float(ev["max"])
        elif op == "equals":
            ok = value == ev["value"]
        elif op == "regex":
            import re
            ok = re.search(ev["pattern"], str(value)) is not None
        else:
            return "pending"
    except (TypeError, ValueError):
        return "auto_fail"
    return "auto_pass" if ok else "auto_fail"


# ---------------------------------------------------------------- checks

def check_profiles(sources, rehash):
    errors, profiles = [], {}
    seen_active = {}

    for path in sorted(REQ_DIR.glob("*.json")):
        p = json.loads(path.read_text(encoding="utf-8"))
        name = path.name
        profiles[p["id"]] = p

        for field in ("schemaVersion", "id", "version", "status", "sourceId", "effectiveFrom", "rules"):
            if field not in p:
                errors.append(f"{name}: нет обязательного поля {field}")
        if errors and p.get("id") is None:
            continue

        if p.get("sourceId") not in sources:
            errors.append(f"{name}: sourceId {p.get('sourceId')!r} отсутствует в sources/index.json")

        if p.get("status") == "active":
            key = (p.get("jurisdiction"), p.get("modelClass"))
            if key in seen_active:
                errors.append(f"{name}: вторая активная версия для {key}, уже есть {seen_active[key]}")
            seen_active[key] = name

        # selfHash. Пересчёт разрешён только для draft: опубликованный профиль неизменяем.
        actual = self_hash(p)
        if rehash and p.get("status") == "draft":
            if p.get("selfHash") != actual:
                p["selfHash"] = actual
                path.write_text(json.dumps(p, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(f"  пересчитан selfHash: {name}")
        elif rehash and p.get("selfHash") != actual:
            errors.append(f"{name}: status={p.get('status')}, пересчёт selfHash запрещён — выпустите новую версию")
        elif p.get("selfHash") != actual:
            errors.append(
                f"{name}: selfHash не совпадает — профиль правили на месте. "
                f"Опубликованный профиль неизменяем: выпустите новую версию."
            )

        ids = set()
        for r in p["rules"]:
            rid = r.get("id", "<без id>")
            for field in REQUIRED_RULE_FIELDS:
                if field not in r:
                    errors.append(f"{name}/{rid}: нет поля {field}")
            if rid in ids:
                errors.append(f"{name}: дубликат id правила {rid}")
            ids.add(rid)

            if r.get("verification") not in VERIFICATION:
                errors.append(f"{name}/{rid}: verification={r.get('verification')!r} вне {sorted(VERIFICATION)}")
            if r.get("severity") not in SEVERITY:
                errors.append(f"{name}/{rid}: severity={r.get('severity')!r} вне {sorted(SEVERITY)}")
            if "status" in r and r["status"] not in RULE_STATUS:
                errors.append(f"{name}/{rid}: status={r['status']!r} вне {sorted(RULE_STATUS)}")
            if r.get("status") == "pending_activation" and not r.get("activationCondition"):
                errors.append(f"{name}/{rid}: pending_activation без activationCondition")

            ev = r.get("evaluator")
            if ev and r.get("verification") != "automatic":
                errors.append(f"{name}/{rid}: evaluator при verification={r.get('verification')}")
            if r.get("verification") == "automatic" and not ev:
                errors.append(f"{name}/{rid}: automatic без evaluator")
            if ev and ev.get("op") not in OPS:
                errors.append(f"{name}/{rid}: неизвестная операция {ev.get('op')!r}")
            if ev and ev.get("op") == "all_zero" and not ev.get("paths"):
                errors.append(f"{name}/{rid}: all_zero без paths")
            if ev and ev.get("op") != "all_zero" and not ev.get("path"):
                errors.append(f"{name}/{rid}: {ev.get('op')} без path")

    return errors, profiles


def check_fixtures(profiles):
    errors = []
    if not FIXTURE_DIR.exists():
        return ["tests/fixtures отсутствует — золотых кейсов нет"]

    for path in sorted(FIXTURE_DIR.glob("*.json")):
        fx = json.loads(path.read_text(encoding="utf-8"))
        profile = profiles.get(fx["profileId"])
        if not profile:
            errors.append(f"{path.name}: профиль {fx['profileId']} не найден")
            continue
        for case in fx["cases"]:
            got = evaluate(profile, case["context"], case.get("decisions"))
            for key, want in case["expect"].items():
                if got.get(key) != want:
                    errors.append(
                        f"{path.name}/{case['name']}: {key} = {got.get(key)}, ожидалось {want}"
                    )
    return errors


def main() -> int:
    rehash = "--rehash" in sys.argv
    sources = json.loads((ROOT / "sources" / "index.json").read_text(encoding="utf-8"))["sources"]

    errors, profiles = check_profiles(sources, rehash)
    if not rehash:
        errors += check_fixtures(profiles)

    total_rules = sum(len(p["rules"]) for p in profiles.values())
    auto = sum(1 for p in profiles.values() for r in p["rules"] if r["verification"] == "automatic")
    print(f"профилей: {len(profiles)} · правил: {total_rules} · автопроверяемых: {auto}")

    if errors:
        print(f"\nОШИБКИ ({len(errors)}):")
        for e in errors:
            print(f"  · {e}")
        return 1
    print("всё сходится")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
