"""Импакт-анализ: какие правила требуют повторной юридической верификации.

Важно, что именно это означает. Изменение источника НЕ означает, что правила
изменились. Оно означает: «источник под этими правилами сдвинулся, человек обязан
пересмотреть их заново».

Обход графа:
    изменившийся source
      → потомки по partOf (нормативный документ внутри другого документа)
      → профили, чей sourceId попал в это множество
      → правила профиля, сужённые impactMap по изменившимся веткам
      + правила с прямым dependsOnSource на любой источник из множества
"""

SEVERITY_ORDER = {"minor": 1, "major": 2, "blocker": 3}

# Профили в этих статусах участвуют в оценке влияния. draft включён намеренно:
# единственный сейчас мониторимый источник ведёт в draft-профиль, и без него
# лестница классификации не имеет достижимых состояний.
COUNTED_PROFILE_STATUSES = {"active", "draft"}


class GraphError(Exception):
    """Реестр источников противоречив — обход невозможен."""


def source_closure(source_id, sources):
    """source_id и все источники, для которых он является предком по partOf.

    Цикл partOf — явная ошибка, а не молчаливый выход: на битом графе импакт-анализ
    обязан падать, а не занижать охват.
    """
    if source_id not in sources:
        raise GraphError(f"источник {source_id!r} отсутствует в реестре")

    # проверка цепочек partOf на циклы
    for start in sources:
        seen, cur = [], start
        while cur is not None:
            if cur in seen:
                raise GraphError("цикл partOf: " + " -> ".join(seen + [cur]))
            seen.append(cur)
            parent = sources.get(cur, {}).get("partOf")
            if parent is not None and parent not in sources:
                raise GraphError(f"{cur}.partOf -> {parent!r} отсутствует в реестре")
            cur = parent

    closure = {source_id}
    changed = True
    while changed:
        changed = False
        for sid, s in sources.items():
            if s.get("partOf") in closure and sid not in closure:
                closure.add(sid)
                changed = True
    return closure


def analyse(source_id, changed_branches, sources, profiles, monitor):
    """Вернуть отчёт о влиянии.

    changed_branches — список изменившихся веток; пустой означает «изменение есть,
    но локализовать его по веткам не удалось», и тогда охват не сужается.
    """
    closure = source_closure(source_id, sources)
    impact_map = (monitor or {}).get("impactMap", {})

    # Ветка offDocs — это реестр реквизитов НПА, а не текст требований.
    # Её изменение сигналит о новой редакции документа, но правил не сужает.
    registry_branches = [b for b in changed_branches if impact_map.get(b) == "__source_registry__"]
    content_branches = [b for b in changed_branches if impact_map.get(b) != "__source_registry__"]

    allowed = None
    if content_branches and impact_map:
        allowed = set()
        for branch in content_branches:
            mapped = impact_map.get(branch)
            if isinstance(mapped, list):
                allowed.update(mapped)
            else:
                # ветка не описана в impactMap — сузить нельзя, берём профиль целиком
                allowed = None
                break

    affected_profiles, affected_rules, max_sev = [], [], None
    for pid, profile in sorted(profiles.items()):
        if profile.get("status") not in COUNTED_PROFILE_STATUSES:
            continue

        by_source = profile.get("sourceId") in closure
        hits = []
        for rule in profile.get("rules", []):
            # правила, которые ещё не вступили в силу, влияние не поднимают
            if rule.get("status") == "pending_activation":
                continue
            direct = rule.get("dependsOnSource") in closure
            via_profile = by_source and (allowed is None or rule["id"] in allowed)
            if direct or via_profile:
                hits.append(rule)

        if not hits:
            continue
        affected_profiles.append(pid)
        for rule in hits:
            affected_rules.append({"profileId": pid, "ruleId": rule["id"], "severity": rule["severity"], "title": rule["title"]})
            sev = rule["severity"]
            if max_sev is None or SEVERITY_ORDER.get(sev, 0) > SEVERITY_ORDER.get(max_sev, 0):
                max_sev = sev

    return {
        "sourceClosure": sorted(closure),
        "changedBranches": list(changed_branches),
        "registryBranches": registry_branches,
        "narrowedByImpactMap": allowed is not None,
        "affectedProfiles": affected_profiles,
        "affectedRules": affected_rules,
        "maxSeverity": max_sev,
    }


def classify(event, impact):
    """Детерминированная классификация. LLM здесь не участвует.

    Решение «юридически существенное изменение или нет» принимает человек после
    просмотра diff. Здесь решается только, кого и насколько срочно звать.
    """
    if event in ("UNAVAILABLE", "PARSER_ERROR", "BASELINE_STALE"):
        return "TECHNICAL_FAILURE"
    if event in ("UNCHANGED", "BASELINE_MISSING"):
        return "INFORMATIONAL"
    # event == CHANGED
    if impact and impact.get("maxSeverity") == "blocker":
        return "CRITICAL_REVIEW"
    if impact and impact.get("affectedRules"):
        return "REVIEW_REQUIRED"
    return "INFORMATIONAL"
