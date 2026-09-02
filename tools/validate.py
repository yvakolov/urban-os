#!/usr/bin/env python3
"""Проверка целостности реестра требований и прогон золотых фикстур.

    python3 tools/validate.py            проверить
    python3 tools/validate.py --rehash   пересчитать selfHash (только для draft)

Без зависимостей. Ненулевой код возврата = что-то сломано.
В сеть не ходит никогда — это инвариант, на нём держится воспроизводимость CI.

Порядок: структурная валидация по schemas/*.schema.json, затем семантические
проверки, которые JSON Schema выразить не может, затем ссылочная целостность,
затем фикстуры.
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import schema as jsonschema  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REQ_DIR = ROOT / "requirements"
FIXTURE_DIR = ROOT / "tests" / "fixtures"
SCHEMA_DIR = ROOT / "schemas"
SOURCES_FILE = ROOT / "sources" / "index.json"
MONITOR_FILE = ROOT / "monitoring" / "sources.json"
DELIVERABLES_FILE = ROOT / "deliverables" / "index.json"

PROFILE_SCHEMA = jsonschema.load(SCHEMA_DIR / "requirement-profile.schema.json")
REGISTRY_SCHEMA = jsonschema.load(SCHEMA_DIR / "source-registry.schema.json")
MONITOR_SCHEMA = jsonschema.load(SCHEMA_DIR / "monitor-config.schema.json")

# Допустимые значения берём из схемы, а не из локальных констант: иначе они
# разъезжаются между двумя источниками правды.
VERIFICATION = jsonschema.enum_of(PROFILE_SCHEMA, "$defs/rule/properties/verification")
SEVERITY = jsonschema.enum_of(PROFILE_SCHEMA, "$defs/rule/properties/severity")
RULE_STATUS = jsonschema.enum_of(PROFILE_SCHEMA, "$defs/rule/properties/status")
OPS = jsonschema.enum_of(PROFILE_SCHEMA, "$defs/evaluator/properties/op")
PROFILE_STATUS = jsonschema.enum_of(PROFILE_SCHEMA, "properties/status")

REQUIRED_RULE_FIELDS = ("id", "category", "title", "requirement", "verification", "severity", "sourceRef")


# Поля, не входящие в selfHash. status исключён намеренно: он описывает место
# профиля в жизненном цикле draft -> active -> superseded, а не его содержание.
# Пока он входил в хеш, объявленный жизненный цикл был физически невозможен —
# перевод активной редакции в superseded ломал её собственный хеш, а править
# опубликованный профиль запрещено. Хеш обязан отвечать на вопрос «то же ли это
# требование», а не «в каком оно сейчас статусе».
HASH_EXCLUDED = ("selfHash", "status")


def self_hash(profile: dict) -> str:
    """sha256 канонического JSON профиля без selfHash и status."""
    body = {k: v for k, v in profile.items() if k not in HASH_EXCLUDED}
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


def evaluate(profile: dict, context: dict, decisions: dict | None = None,
             source_review_current: bool | None = None) -> dict:
    """Портированный evaluateProfile. Порядок ветвлений повторяет core.js."""
    decisions = decisions or {}
    counts = dict.fromkeys(
        ("auto_pass", "auto_fail", "expert_reviewed", "not_applicable",
         "pending", "awaiting_external", "rule_error"), 0
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
        and counts["rule_error"] == 0
    )
    # Свежесть пересмотра — свойство профиля, а не объекта, поэтому приходит
    # снаружи. Источник сдвинулся, а человек правила не перечитал — выпускать
    # по ним нельзя, сколько бы проверок ни прошло: они могли устареть все
    # разом. None означает «не спрашивали» и поведения не меняет.
    counts["sourceReviewCurrent"] = source_review_current
    if source_review_current is False:
        counts["releaseEligible"] = False
        counts["releaseBlockedBy"] = "source_review_stale"
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
        try:
            return "auto_pass" if all(float(v) == 0 for v in vals) else "auto_fail"
        except (TypeError, ValueError):
            return "rule_error"

    value = get_path(context, ev.get("path"))
    if is_missing(value):
        return "pending"

    # Сломанный evaluator — это дефект правила, а не несоответствие объекта.
    # Выдавать его за auto_fail недопустимо: в комплаенс-инструменте это означало бы
    # обвинить заявителя в нарушении, которого он не совершал.
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
            return "rule_error"
    except (TypeError, ValueError, KeyError):
        return "rule_error"
    return "auto_pass" if ok else "auto_fail"


# ---------------------------------------------------------------- структура

def check_structure(sources_doc, monitor_doc, raw_profiles):
    errors = []
    for msg in jsonschema.validate(sources_doc, REGISTRY_SCHEMA):
        errors.append(f"sources/index.json {msg}")
    if monitor_doc is not None:
        for msg in jsonschema.validate(monitor_doc, MONITOR_SCHEMA):
            errors.append(f"monitoring/sources.json {msg}")
    for name, doc in raw_profiles:
        for msg in jsonschema.validate(doc, PROFILE_SCHEMA):
            errors.append(f"{name} {msg}")
    return errors


# ---------------------------------------------------------------- профили

def check_profiles(sources, raw_profiles, rehash):
    errors, warnings, profiles = [], [], {}
    seen_active = {}
    # Один id — несколько редакций транскрипции одного и того же источника.
    # Индексировать по id нельзя: вторая версия молча затёрла бы первую, и
    # проверки ссылочной целостности пошли бы по чужому набору правил.
    seen_versions = {}
    live = {}

    for path, p in raw_profiles:
        name = path.name if hasattr(path, "name") else str(path)

        missing = [f for f in ("schemaVersion", "id", "version", "status", "sourceId", "effectiveFrom", "rules") if f not in p]
        if missing:
            errors.append(f"{name}: нет обязательных полей: {', '.join(missing)}")
            # без id профиль в индекс не кладём — иначе дальше KeyError вместо диагностики
            if "id" not in p:
                continue
        key = (p["id"], p.get("version"))
        if key in seen_versions:
            errors.append(
                f"{name}: {p['id']} версии {p.get('version')} уже объявлен в "
                f"{seen_versions[key]} — две редакции с одним номером неразличимы")
        seen_versions[key] = name
        if p.get("status") != "superseded":
            live.setdefault(p["id"], []).append(name)
            profiles[p["id"]] = p
        else:
            profiles.setdefault(p["id"], p)

        if p.get("sourceId") not in sources:
            errors.append(f"{name}: sourceId {p.get('sourceId')!r} отсутствует в sources/index.json")

        if p.get("status") == "active":
            key = (p.get("jurisdiction"), p.get("modelClass"))
            seen_active.setdefault(key, []).append(name)

        actual = self_hash(p)
        if rehash and p.get("status") == "draft":
            if p.get("selfHash") != actual:
                p["selfHash"] = actual
                Path(path).write_text(json.dumps(p, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(f"  пересчитан selfHash: {name}")
        elif rehash and p.get("selfHash") != actual:
            errors.append(f"{name}: status={p.get('status')}, пересчёт selfHash запрещён — выпустите новую версию")
        elif p.get("selfHash") != actual:
            errors.append(
                f"{name}: selfHash не совпадает — профиль правили на месте. "
                f"Опубликованный профиль неизменяем: выпустите новую версию."
            )

        ids = set()
        for r in p.get("rules", []):
            rid = r.get("id", "<без id>")
            for field in REQUIRED_RULE_FIELDS:
                if field not in r:
                    errors.append(f"{name}/{rid}: нет поля {field}")
            if rid in ids:
                errors.append(f"{name}: дубликат id правила {rid}")
            ids.add(rid)

            if r.get("status") == "pending_activation" and not r.get("activationCondition"):
                errors.append(f"{name}/{rid}: pending_activation без activationCondition")

            ev = r.get("evaluator")
            if ev and r.get("verification") != "automatic":
                errors.append(f"{name}/{rid}: evaluator при verification={r.get('verification')}")
            if r.get("verification") == "automatic" and not ev:
                errors.append(f"{name}/{rid}: automatic без evaluator")
            if ev and ev.get("op") == "all_zero" and not ev.get("paths"):
                errors.append(f"{name}/{rid}: all_zero без paths")
            if ev and ev.get("op") not in (None, "all_zero") and not ev.get("path"):
                errors.append(f"{name}/{rid}: {ev.get('op')} без path")

    for pid, names in live.items():
        if len(names) > 1:
            errors.append(
                f"{pid}: сразу {len(names)} не перекрытых редакций ({', '.join(sorted(names))}) — "
                f"предыдущая должна получить status=superseded")

    for key, names in seen_active.items():
        if len(names) > 1:
            errors.append(
                f"несколько активных версий для {key}: " + ", ".join(sorted(names))
            )

    return errors, warnings, profiles


# ------------------------------------------------------- ссылочная целостность

def check_references(sources_doc, profiles, monitor_doc):
    """Импакт-анализ ходит по этому графу, поэтому граф обязан быть связным."""
    errors, warnings = [], []
    sources = sources_doc["sources"]
    authorities = sources_doc.get("authorities", {})

    for sid, s in sources.items():
        for field in ("partOf", "supersedes", "supersededBy"):
            target = s.get(field)
            if target and target not in sources:
                errors.append(f"sources/{sid}.{field} -> {target!r} отсутствует в реестре")

        back = s.get("supersedes")
        if back and back in sources and sources[back].get("supersededBy") not in (None, sid):
            errors.append(
                f"sources/{sid}.supersedes -> {back}, но {back}.supersededBy = "
                f"{sources[back].get('supersededBy')!r} — связь несимметрична"
            )

        for pid in s.get("transcribedInto", []) or []:
            if pid not in profiles:
                errors.append(f"sources/{sid}.transcribedInto -> {pid!r} — такого профиля нет")
            elif profiles[pid].get("sourceId") != sid:
                errors.append(
                    f"sources/{sid}.transcribedInto -> {pid}, но {pid}.sourceId = "
                    f"{profiles[pid].get('sourceId')!r} — связь несогласована"
                )

    # циклы partOf
    for start in sources:
        seen, cur = [], start
        while cur is not None:
            if cur in seen:
                errors.append("цикл partOf: " + " -> ".join(seen + [cur]))
                break
            seen.append(cur)
            cur = sources.get(cur, {}).get("partOf")

    for pid, p in profiles.items():
        sup = p.get("supersedes")
        if sup and sup not in profiles:
            # Предыдущая редакция реально существовала, просто не оцифрована.
            # Это констатация истории, а не дефект — warning, иначе CI красный на ровном месте.
            warnings.append(f"{pid}.supersedes -> {sup!r} — профиль не оцифрован")
        own_rule_ids = {r["id"] for r in p.get("rules", []) if "id" in r}
        for r in p.get("rules", []):
            for dep in r.get("dependsOn", []) or []:
                if dep not in profiles:
                    errors.append(f"{pid}/{r['id']}.dependsOn -> {dep!r} — такого профиля нет")
            # elaborates связывает пункт с пунктом внутри одного документа,
            # поэтому цель обязана лежать в этом же профиле.
            for el in r.get("elaborates", []) or []:
                if el not in own_rule_ids:
                    errors.append(f"{pid}/{r['id']}.elaborates -> {el!r} — правила нет в этом профиле")
                elif el == r["id"]:
                    errors.append(f"{pid}/{r['id']}.elaborates ссылается само на себя")
            dsrc = r.get("dependsOnSource")
            if dsrc and dsrc not in sources:
                errors.append(f"{pid}/{r['id']}.dependsOnSource -> {dsrc!r} — нет в реестре")
            auth = r.get("authority")
            if auth and auth not in authorities:
                errors.append(f"{pid}/{r['id']}.authority -> {auth!r} — нет в authorities")

    if monitor_doc:
        rule_index = {(pid, r["id"]) for pid, p in profiles.items() for r in p.get("rules", [])}
        all_rule_ids = {r["id"] for p in profiles.values() for r in p.get("rules", [])}
        for sid, mon in monitor_doc.get("monitors", {}).items():
            if sid not in sources:
                errors.append(f"monitoring/{sid} -> источника нет в sources/index.json")
            for branch, mapped in (mon.get("impactMap") or {}).items():
                if isinstance(mapped, list):
                    for rid in mapped:
                        if rid not in all_rule_ids:
                            errors.append(f"monitoring/{sid}.impactMap.{branch} -> правила {rid!r} не существует")
        del rule_index

    # sha256 файлов источников
    for sid, s in sources.items():
        f = s.get("file")
        if not f:
            continue
        candidate = ROOT / f
        if not candidate.exists():
            warnings.append(
                f"sources/{sid}: файл {f} недоступен из репозитория — sha256 объявлен, но не проверен"
            )
            continue
        declared = s.get("sha256")
        if not declared:
            warnings.append(f"sources/{sid}: файл есть, но sha256 не объявлен")
            continue
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != declared:
            errors.append(f"sources/{sid}: sha256 файла {actual} != объявленного {declared}")

    return errors, warnings


# ---------------------------------------------------------------- фикстуры

def check_orphan_profiles(profiles, deliverables):
    """Профиль, из которого не порождается ни один артефакт комплекта.

    Каждое требование существует ради сущности, которую надо предъявить.
    Корпус, не связанный ни с одним артефактом, либо лежит не в своём реестре,
    либо потерял связь — и то и другое стоит увидеть, а не обнаружить глазами
    на картинке. Предупреждение, не ошибка: у корпуса другой юрисдикции
    отсутствие связи с московским комплектом закономерно.
    """
    linked = set()
    for spec in deliverables.values():
        if spec.get("profile"):
            linked.add(spec["profile"])
    rule_owner = {r["id"]: pid for pid, p in profiles.items() for r in p.get("rules", [])}
    for spec in deliverables.values():
        for rid in spec.get("producedBy") or []:
            if rid in rule_owner:
                linked.add(rule_owner[rid])
    out = []
    for pid, p in sorted(profiles.items()):
        if p.get("status") != "active" or pid in linked:
            continue
        # Эксплуатационный контур артефактов комплекта и не порождает: он
        # действует после выдачи свидетельства. Объявленное отсутствие связи
        # предупреждением быть не должно — иначе оно приучает к шуму.
        if p.get("contour") == "operational":
            continue
        out.append(f"{pid}: не порождает ни одного артефакта комплекта — "
                   f"проверьте, в своём ли он реестре")
    return out


def check_review(profiles):
    """Свежесть пересмотра — предупреждение, а не ошибка сборки.

    Непересмотренный источник блокирует ВЫПУСК, а не сборку: красить CI из-за
    того, что человек ещё не сел читать, значит приучить смотреть на красное
    как на норму. Блокировка живёт там, где принимается решение о выпуске.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import review as review_mod
    except Exception as exc:
        return [f"состояние пересмотра не прочитано: {type(exc).__name__}: {exc}"]
    state, sources, active = review_mod.load()
    rows = review_mod.status(state, sources, active)
    out = []
    for pid, sid, st, _cur, _seen in rows:
        if st == review_mod.NEVER:
            out.append(f"{pid}: источник {sid} ни разу не пересматривался человеком — выпуск заблокирован")
        elif st == review_mod.STALE:
            out.append(f"{pid}: источник {sid} сдвинулся после последнего пересмотра — выпуск заблокирован")
    return out


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
        # Кейсы пинятся к версии: новая редакция транскрипции меняет способ
        # проверки, и молча пересчитывать под неё эталонные числа — значит
        # потерять сам смысл эталона.
        want_version = fx.get("profileVersion")
        if want_version and profile.get("version") != want_version:
            errors.append(
                f"{path.name}: кейсы сняты с версии {want_version}, "
                f"а активна {profile.get('version')} — перепроверьте и обновите ожидания")
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

    sources_doc = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    monitor_doc = json.loads(MONITOR_FILE.read_text(encoding="utf-8")) if MONITOR_FILE.exists() else None
    deliverables_doc = (json.loads(DELIVERABLES_FILE.read_text(encoding="utf-8"))
                        if DELIVERABLES_FILE.exists() else {})
    raw_profiles = [(p, json.loads(p.read_text(encoding="utf-8"))) for p in sorted(REQ_DIR.glob("*.json"))]

    errors = check_structure(sources_doc, monitor_doc, [(p.name, d) for p, d in raw_profiles])
    warnings = []

    perr, pwarn, profiles = check_profiles(sources_doc["sources"], raw_profiles, rehash)
    errors += perr
    warnings += pwarn

    rerr, rwarn = check_references(sources_doc, profiles, monitor_doc)
    errors += rerr
    warnings += rwarn

    if not rehash:
        errors += check_fixtures(profiles)
        warnings += check_review(profiles)
        warnings += check_orphan_profiles(profiles, deliverables_doc.get("deliverables", {}))

    total_rules = sum(len(p.get("rules", [])) for p in profiles.values())
    auto = sum(1 for p in profiles.values() for r in p.get("rules", []) if r.get("verification") == "automatic")
    monitored = len((monitor_doc or {}).get("monitors", {}))
    print(
        f"источников: {len(sources_doc['sources'])} · профилей: {len(profiles)} · "
        f"правил: {total_rules} · автопроверяемых: {auto} · под наблюдением: {monitored}"
    )

    if warnings:
        print(f"\nПРЕДУПРЕЖДЕНИЯ ({len(warnings)}):")
        for w in warnings:
            print(f"  · {w}")

    if errors:
        print(f"\nОШИБКИ ({len(errors)}):")
        for e in errors:
            print(f"  · {e}")
        return 1
    print("\nвсё сходится")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
