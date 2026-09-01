"""Инспектор комплекта запроса: что подаётся в Департамент.

Меряет то, на чём отказывают НА ПРИЁМЕ, до содержательного рассмотрения:
полнота комплекта, форматы файлов, наличие подписей, сроки документов.
Правил не знает — только считает факты.

Работает с манифестом, а не с каталогом на диске: манифест воспроизводим,
его можно положить в фикстуру, и проверка не зависит от того, где лежат
файлы заявителя.

Манифест:
    {"documents": {"<deliverableId>": [{"name": "...", "bytes": 123}, ...]},
     "form": {"sections": ["1", "1.1", "3", ...]},   # заполненные разделы приложения 2
     "objectSignificance": "district" | "city",      # значение объекта, п. 2.7.1
     "gpzu": {"validUntil": "2026-12-31"},
     "serviceTermWorkingDays": 20,
     "asOf": "2026-09-01"}
"""

import datetime as dt
import os
import re

INSPECTION_VERSION = 1

SIGNATURE_EXT = {"sig", "sgn", "p7s", "sign"}


def _ext(name):
    return os.path.splitext(name)[-1].lstrip(".").lower()


def _stem(name):
    return os.path.splitext(name)[0]


def _working_days_between(a, b):
    """Рабочих дней от a до b. Праздники не учитываются — только выходные.

    Ceiling: производственный календарь сюда не подключён, поэтому оценка
    оптимистична на число праздников в интервале. Для проверки «успеет ли
    ГПЗУ дожить до конца рассмотрения» это работает в опасную сторону —
    занижает риск, — поэтому запас берётся с двойным сроком услуги.
    """
    if b <= a:
        return 0
    days = 0
    cur = a
    while cur < b:
        cur += dt.timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def service_term(manifest, terms):
    """Срок услуги в рабочих днях по п. 2.7.1 Регламента.

    Срок не один: 10 дней для объектов окружного значения, 20 для городского,
    15 при обращении фондов реновации и защиты прав дольщиков. Брать 20 всегда
    было бы неверно для окружных объектов, а брать 10 — опасно: проверка
    «доживёт ли ГПЗУ до конца рассмотрения» при заниженном сроке пропустит
    документ, который истечёт в ходе рассмотрения.
    """
    if not terms:
        return int(manifest.get("serviceTermWorkingDays") or 20), None
    table = {t["key"]: t["workingDays"] for t in terms["terms"]}
    if manifest.get("applicantIsRenovationParty"):
        key = "renovation"
    else:
        key = manifest.get("objectSignificance") or terms["defaultKey"]
    return table.get(key, table[terms["defaultKey"]]), key


def inspect(manifest, deliverables, form_sections=None, terms=None):
    """manifest + реестр артефактов (+ словарь разделов формы) -> плоские факты.

    form_sections — dictionaries/request-form-sections.v1.json. Инспектор из
    него берёт только перечень разделов: какие из них применимы к конкретному
    заявителю, решают правила профиля через appliesWhen, а не он.
    """
    docs = manifest.get("documents") or {}
    as_of = dt.date.fromisoformat(manifest.get("asOf") or dt.date.today().isoformat())
    term, term_key = service_term(manifest, terms)

    per_doc = {}
    unreadable = 0
    signature_mismatch = 0

    for did, spec in deliverables.items():
        accepted = spec.get("accepted")
        if accepted is None:
            continue  # артефакт без объявленных форматов не проверяем
        files = docs.get(did) or []
        payload = [f for f in files if _ext(f["name"]) not in SIGNATURE_EXT]
        sigs = [f for f in files if _ext(f["name"]) in SIGNATURE_EXT]

        present = bool(payload)
        container = spec.get("container")
        # Файл принимается, если его расширение среди допустимых либо совпадает
        # с контейнером: СПОЗУ подаётся как IFC внутри ZIP — снаружи виден ZIP.
        allowed = set(accepted) | ({container} if container else set())
        wrong = [f["name"] for f in payload if _ext(f["name"]) not in allowed]
        if wrong:
            unreadable += len(wrong)

        sig_ok = True
        if spec.get("signatureRequired") and present:
            stems = {_stem(f["name"]) for f in payload}
            sig_stems = {_stem(f["name"]) for f in sigs}
            # п. 4.2.3 и требования к НПМ/ВПМ: имя подписи совпадает с именем файла
            sig_ok = bool(sig_stems) and stems <= sig_stems
            if not sig_ok:
                signature_mismatch += 1

        over = spec.get("maxBytes") and any(f.get("bytes", 0) > spec["maxBytes"] for f in payload)

        per_doc[did] = {
            "present": present,
            "files": len(payload),
            "formatValid": present and not wrong,
            "wrongFormat": wrong,
            "signatureOk": sig_ok,
            "sizeOk": not over,
            "accepted": sorted(allowed),
        }

    # Пустой accepted означает, что файла нет по определению — запрос подаётся
    # интерактивной формой на Портале. Считать его недостающим ДОКУМЕНТОМ нельзя:
    # пофайловые проверки к нему неприменимы.
    mandatory = [d for d, s in deliverables.items()
                 if s.get("accepted") and s.get("origin") in ("applicant", "produced")]
    present_count = sum(1 for d in mandatory if per_doc.get(d, {}).get("present"))

    # Но в общий объём данных комплекта форма входит: сведения по приложению 2
    # — такая же часть запроса, как приложенные документы. Поэтому единицы
    # данных считаются отдельно от файлов и включают носителей без файлов.
    form_filled = set((manifest.get("form") or {}).get("sections") or [])
    carriers = [d for d, s in deliverables.items()
                if s.get("carriesData") and s.get("origin") in ("applicant", "produced")]
    data_items = mandatory + carriers
    data_missing = sorted(
        [d for d in mandatory if not per_doc.get(d, {}).get("present")]
        + [d for d in carriers if not form_filled])

    facts = {
        "inspectionVersion": INSPECTION_VERSION,
        "documents": per_doc,
        "mandatoryRequired": len(mandatory),
        "mandatoryPresent": present_count,
        "mandatoryMissing": sorted(d for d in mandatory if not per_doc.get(d, {}).get("present")),
        "mandatoryMissingCount": sum(1 for d in mandatory if not per_doc.get(d, {}).get("present")),
        "unreadable": unreadable,
        "signatureMismatch": signature_mismatch,
        "allFormatsValid": all(v["formatValid"] for v in per_doc.values() if v["present"]),
        "dataItemsRequired": len(data_items),
        "dataItemsPresent": len(data_items) - len(data_missing),
        "dataItemsMissing": data_missing,
        "dataItemsMissingCount": len(data_missing),
        "form": _form_facts(form_filled, form_sections),
    }

    gpzu = manifest.get("gpzu") or {}
    if gpzu.get("validUntil"):
        until = dt.date.fromisoformat(gpzu["validUntil"])
        remaining = _working_days_between(as_of, until)
        facts["gpzu"] = {
            "validUntil": gpzu["validUntil"],
            "workingDaysRemaining": remaining,
            "serviceTermWorkingDays": term,
            "serviceTermBasis": term_key,
            # Запас двойной: ГПЗУ не должен истечь В ХОДЕ рассмотрения
            # (основание для отказа, предоставление п. 13), а производственный
            # календарь здесь не учитывается — см. ceiling в _working_days_between.
            "survivesReview": remaining >= term * 2,
        }
    return facts


def manifest_from_directory(path, mapping):
    """Собрать манифест из каталога. mapping: подкаталог -> deliverableId."""
    root = os.path.abspath(path)
    docs = {}
    for sub, did in mapping.items():
        d = os.path.join(root, sub)
        if not os.path.isdir(d):
            continue
        docs[did] = [{"name": n, "bytes": os.path.getsize(os.path.join(d, n))}
                     for n in sorted(os.listdir(d)) if os.path.isfile(os.path.join(d, n))]
    return {"documents": docs}


def _form_facts(filled, dictionary):
    """Какие разделы приложения 2 заполнены. Обязательность здесь не решается.

    Ключи разделов идут с дефисом вместо точки: пути evaluator-ов разбираются
    по точкам, и «1.1» распалось бы на два уровня.
    """
    facts = {"present": bool(filled), "sectionsFilled": len(filled)}
    if not dictionary:
        return facts
    sections = {}
    for sec in dictionary.get("sections", []):
        entries = sec.get("subsections") or [sec]
        for e in entries:
            num = e["number"]
            sections[num.replace(".", "-")] = {
                "number": num,
                "present": num in filled,
                "applicability": e.get("applicability"),
            }
    facts["sections"] = sections
    facts["sectionsKnown"] = len(sections)
    facts["unknownSectionsFilled"] = sorted(
        n for n in filled if n.replace(".", "-") not in sections)
    return facts

