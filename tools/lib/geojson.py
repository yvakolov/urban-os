"""Инспектор GeoJSON пакета ВПМ и рекламных конструкций.

Меряет то, что задано полем: синтаксис, кодировку, запись чисел, точность
координат и наличие обязательных полей. Обязательность берётся из словаря
dictionaries/geojson-fields.v1.json, а не зашита здесь.

Ключевое ограничение и то, как оно обходится. Полный перечень полей задан
приложением 10 к требованиям ВПМ, а этого документа в репозитории нет — в
профилях названы только те поля, что упомянуты в тексте правил. Поэтому
полнота выражается тремя значениями:

    False  — известное обязательное поле отсутствует. Нарушение доказано.
    None   — все известные поля на месте, но перечень неполон. Ничего не
             доказано, и выдавать это за соответствие нельзя.
    True   — перечень в словаре объявлен полным и целиком удовлетворён.

Так проверка ловит реальные нарушения, не выдавая незнание за соответствие.
"""

import json
import re

INSPECTION_VERSION = 1

# Строка, которая выглядит числом. п. 2.5: числовые поля записываются числами,
# а не строками, и разделителем служит точка.
def _looks_numeric(value):
    if not isinstance(value, str):
        return False
    s = value.strip().replace(",", ".")
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def _decimals(token):
    s = str(token)
    return len(s.split(".", 1)[1]) if "." in s else 0


class _DupTracker:
    """object_pairs_hook, замечающий повторяющиеся ключи.

    json.loads молча оставляет последнее значение, поэтому дубликат имени поля
    без этого не отличить от его отсутствия.
    """

    def __init__(self):
        self.duplicates = []

    def __call__(self, pairs):
        seen = set()
        for k, _v in pairs:
            if k in seen:
                self.duplicates.append(k)
            seen.add(k)
        return dict(pairs)


def parse(raw):
    """bytes -> (данные, факты о разборе). Ошибка разбора не исключение, а факт.

    Возвращается и разобранный текст: часть требований предъявлена к записи
    чисел, а не к их значению, и по разобранным данным не проверяется.
    """
    facts = {"utf8": True, "parsed": True, "duplicateFields": [], "error": None}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, "", {**facts, "utf8": False, "parsed": False, "error": str(exc)}
    tracker = _DupTracker()
    try:
        data = json.loads(text, object_pairs_hook=tracker)
    except ValueError as exc:
        return None, text, {**facts, "parsed": False, "error": str(exc)}
    facts["duplicateFields"] = sorted(set(tracker.duplicates))
    return data, text, facts


def _properties(data):
    """Свойства всех объектов, независимо от того, Feature это или коллекция."""
    if not isinstance(data, dict):
        return []
    if data.get("type") == "FeatureCollection":
        return [f.get("properties") or {} for f in (data.get("features") or [])
                if isinstance(f, dict)]
    if data.get("type") == "Feature":
        return [data.get("properties") or {}]
    # Требования ВПМ описывают плоский объект с полями, а не строгий GeoJSON:
    # если типа нет, считаем сам объект носителем свойств.
    return [data]


COORD_RE = re.compile(r'"coordinates"\s*:\s*\[([^\]\[]*)\]')


def _coordinate_tokens(text):
    """Координаты точки вставки как они ЗАПИСАНЫ, а не как разобраны.

    Требование предъявлено к записи — «ровно 3 знака после точки». Разбор JSON
    эту запись теряет: 2.500 становится числом 2.5, и проверить требование по
    разобранному значению уже нельзя. Поэтому литералы берутся из текста.
    """
    out = []
    for m in COORD_RE.finditer(text):
        out.extend(t.strip() for t in m.group(1).split(",") if t.strip())
    return out


def inspect(raw, dictionary=None, scope=None):
    """raw bytes GeoJSON (+ словарь полей, + область: oks | ground | advertising)."""
    data, text, parse_facts = parse(raw)
    facts = {
        "inspectionVersion": INSPECTION_VERSION,
        "utf8": parse_facts["utf8"],
        "parsed": parse_facts["parsed"],
        "duplicateFields": parse_facts["duplicateFields"],
        "parseError": parse_facts["error"],
    }
    if data is None:
        facts["valid"] = False
        return facts

    props = _properties(data)
    numeric_as_string = sorted({
        k for p in props for k, v in p.items() if _looks_numeric(v)})
    facts["featureCount"] = len(props)
    facts["numericFieldsAsString"] = numeric_as_string
    facts["valid"] = (parse_facts["utf8"] and parse_facts["parsed"]
                      and not parse_facts["duplicateFields"]
                      and not numeric_as_string)

    tokens = _coordinate_tokens(text)
    facts["coordinateCount"] = len(tokens)
    if tokens:
        decs = [_decimals(t) for t in tokens]
        facts["coordinateDecimals"] = sorted(set(decs))
        # «Ровно 3 знака», не «не менее»: и 2 знака, и 5 — нарушение.
        facts["coordinatesPrecision3"] = all(d == 3 for d in decs)

    if dictionary:
        scopes = dictionary.get("scopes") or {}
        for name in ((scope,) if scope else tuple(scopes)):
            spec = scopes.get(name)
            if not spec:
                continue
            excluded = set(spec.get("excluded") or [])
            required = [f for f in spec.get("fields") or [] if f not in excluded]
            if props:
                missing = sorted({f for f in required for p in props
                                  if p.get(f) in (None, "")})
            else:
                missing = sorted(required)
            facts[f"{name}MissingFields"] = missing
            facts[f"{name}RequiredComplete"] = (
                False if missing
                else (True if spec.get("completeness") == "full" else None))
            facts[f"{name}FieldListCompleteness"] = spec.get("completeness")

    # Поле Glasses по требованиям к рекламным конструкциям
    facts["hasGlasses"] = any("Glasses" in p for p in props)
    return facts
