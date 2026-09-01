"""Инспектор файлов IFC: разбор STEP Physical File и сбор фактов для правил.

Как и остальные инспекторы, он НЕ знает правил — он только измеряет. Пороги
и обязательность живут в requirements/ и dictionaries/.

Почему свой парсер, а не ifcopenshell. Нужен ровно разбор SPF: заголовок,
экземпляры сущностей, наборы свойств и связи. Это примерно двести строк на
стандартной библиотеке. ifcopenshell тянет за собой геометрическое ядро и
бинарные колёса под каждую платформу — для проверки атрибутов это несоразмерная
цена, и она убила бы главное свойство инструмента: он запускается везде, где
есть python3, без установки чего-либо.

Ceiling: разбираются только те конструкции SPF, что встречаются в выгрузках
ЦИМ АГР. Не поддерживаются ссылки на константы (#REF), комментарии внутри
аргументов и множественное наследование в одном экземпляре. Когда встретится
реальный файл, который не разбирается, — падать с PARSE_ERROR, а не угадывать.
"""

import re

INSPECTION_VERSION = 1

HEADER_RE = re.compile(r"\bISO-10303-21\s*;", re.I)
SCHEMA_RE = re.compile(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']*)'", re.I)
VIEW_RE = re.compile(r"ViewDefinition\s*\[([^\]]*)\]", re.I)
# #12= IFCWALL(...);  — имя сущности всегда до открывающей скобки
STMT_RE = re.compile(r"#(\d+)\s*=\s*([A-Za-z0-9_]+)\s*\(", re.S)
# п. 4.7.8.3 — запрещённые символы в наименовании уровня
BAD_LEVEL_CHARS = re.compile(r"""[!.«»#;%:^?&*()\[\]{}='`~\\]""")


class IfcError(Exception):
    """Файл не разбирается как IFC SPF."""


# ---------------------------------------------------------------- разбор

def _split_args(src):
    """Разбить строку аргументов верхнего уровня по запятым.

    Учитывает вложенные скобки и строки в апострофах, где '' — экранированный
    апостроф. Наивный split(',') ломается на первом же имени с запятой.
    """
    out, buf, depth, in_str = [], [], 0, False
    i = 0
    while i < len(src):
        ch = src[i]
        if in_str:
            if ch == "'":
                if i + 1 < len(src) and src[i + 1] == "'":
                    buf.append("''")
                    i += 2
                    continue
                in_str = False
            buf.append(ch)
        elif ch == "'":
            in_str = True
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf).strip())
    return out


def _unquote(token):
    """'текст' -> текст, с разэкранированием '' и \\X2\\..\\X0\\ (UTF-16 в SPF)."""
    t = token.strip()
    if not (t.startswith("'") and t.endswith("'")):
        return None
    body = t[1:-1].replace("''", "'")

    def x2(m):
        hexes = m.group(1)
        return "".join(chr(int(hexes[i:i + 4], 16)) for i in range(0, len(hexes), 4))

    body = re.sub(r"\\X2\\([0-9A-Fa-f]+)\\X0\\", x2, body)
    body = re.sub(r"\\X\\([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), body)
    return body


def _refs(token):
    return [int(x) for x in re.findall(r"#(\d+)", token)]


def _body_of(text, open_paren_idx):
    """Вернуть содержимое скобок, начинающихся на open_paren_idx."""
    depth, in_str, i = 0, False, open_paren_idx
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren_idx + 1:i]
        i += 1
    raise IfcError("незакрытая скобка в экземпляре сущности")


def parse(text):
    """text -> {'header', 'entities': {id: (TYPE, [args])}}"""
    if not HEADER_RE.search(text[:2000]):
        raise IfcError("нет сигнатуры ISO-10303-21 — это не STEP Physical File")

    schema = SCHEMA_RE.search(text)
    view = VIEW_RE.search(text)
    header = {
        "schema": schema.group(1).strip() if schema else None,
        "mvd": view.group(1).strip() if view else None,
    }

    entities = {}
    for m in STMT_RE.finditer(text):
        eid, etype = int(m.group(1)), m.group(2).upper()
        body = _body_of(text, m.end() - 1)
        entities[eid] = (etype, _split_args(body))
    if not entities:
        raise IfcError("в файле нет ни одного экземпляра сущности")
    return {"header": header, "entities": entities}


# ---------------------------------------------------------------- факты

def _length_unit(entities):
    """Единица длины из IfcUnitAssignment. Правило 4.6.1 требует миллиметры."""
    for etype, args in entities.values():
        if etype != "IFCSIUNIT" or len(args) < 4:
            continue
        # (dimensions, unitType, prefix, name)
        if "LENGTHUNIT" not in args[1].upper():
            continue
        prefix = args[2].strip(".").upper()
        name = args[3].strip(".").upper()
        if name == "METRE":
            return {"MILLI": "MILLIMETRE", "CENTI": "CENTIMETRE", "$": "METRE", "": "METRE"}.get(prefix, prefix + "METRE")
        return name
    return None


def _property_index(entities):
    """objectId -> {(имя_набора, имя_свойства): значение}.

    Связь строится через IfcRelDefinesByProperties: у него предпоследний
    аргумент — список объектов, последний — ссылка на набор свойств.
    """
    psets = {}
    for eid, (etype, args) in entities.items():
        if etype != "IFCPROPERTYSET" or len(args) < 5:
            continue
        name = _unquote(args[2])
        props = {}
        for pid in _refs(args[4]):
            p = entities.get(pid)
            if not p or p[0] != "IFCPROPERTYSINGLEVALUE" or len(p[1]) < 3:
                continue
            pname = _unquote(p[1][0])
            raw = p[1][2].strip()
            val = _unquote(re.sub(r"^[A-Za-z0-9_]+\s*\(", "(", raw).strip("()")) if "(" in raw else None
            if val is None:
                inner = raw[raw.find("(") + 1:raw.rfind(")")] if "(" in raw else raw
                val = _unquote(inner) or inner.strip()
            if pname:
                props[pname] = val
        if name:
            psets[eid] = (name, props)

    index = {}
    for etype, args in entities.values():
        if etype != "IFCRELDEFINESBYPROPERTIES" or len(args) < 6:
            continue
        for pid in _refs(args[5]):
            if pid not in psets:
                continue
            set_name, props = psets[pid]
            for oid in _refs(args[4]):
                bucket = index.setdefault(oid, {})
                for pname, val in props.items():
                    bucket[(set_name, pname)] = val
    return index, psets


def check_level_names(names, grammar):
    """Разобрать наименования уровней по грамматике таблицы 4.3.

    Разбор терпим к отсутствию номера уровня: таблица объявляет поле
    обязательным, но собственный пример источника «С01_крыша_дополнительный»
    его не содержит. Противоречие источника не наше дело разрешать, поэтому
    принимаются обе формы, а несоответствием считается только то, что нарушает
    обе.
    """
    if not grammar:
        return {}
    fields = {f["number"]: f for f in grammar["fields"]}
    sep = grammar["separator"]
    vocab = {v.lower() for v in fields[3]["vocabulary"]}
    purposes = {v.lower() for v in fields[4]["vocabulary"]}
    sub_re = re.compile(fields[1]["pattern"])
    lvl_re = re.compile(fields[2]["pattern"])
    mark_re = re.compile(fields[5]["pattern"])

    bad_subobject, bad_vocabulary, bad_purpose, bad_mark, bad_structure = [], [], [], [], []
    for name in names:
        parts = name.split(sep)
        if len(parts) < 3:
            bad_structure.append(name)
            continue
        if not sub_re.match(parts[0]):
            bad_subobject.append(name)

        # Хвост читается справа: назначение обязательно и всегда последнее,
        # отметка необязательна и может стоять за ним. Разбирать слева нельзя —
        # номер уровня отсутствует у крыши, и позиции полей поехали бы.
        if parts[-1].lower() in purposes:
            middle, mark = parts[1:-1], None
        elif len(parts) >= 4 and parts[-2].lower() in purposes:
            middle, mark = parts[1:-2], parts[-1]
        else:
            bad_purpose.append(name)
            middle, mark = parts[1:-1], None
        if mark is not None and not mark_re.match(mark):
            bad_mark.append(name)

        if len(middle) == 2:            # номер уровня и наименование
            if not lvl_re.match(middle[0]):
                bad_structure.append(name)
            label = middle[1]
        elif len(middle) == 1:          # без номера уровня — форма примера 4.7.9
            label = middle[0]
        else:
            bad_structure.append(name)
            continue

        # в примере 4.7.9 номер и наименование разделены пробелом вместо «_»
        if label.lower() not in vocab:
            head = label.split(" ", 1)
            if not (len(head) == 2 and lvl_re.match(head[0]) and head[1].lower() in vocab):
                bad_vocabulary.append(label)

    out = {
        "checked": len(names),
        "badSubobjectField": sorted(set(bad_subobject)),
        "unknownLevelLabel": sorted(set(bad_vocabulary)),
        "badPurposeField": sorted(set(bad_purpose)),
        "badElevationField": sorted(set(bad_mark)),
        "badStructure": sorted(set(bad_structure)),
    }
    # evaluator-ы сравнивают числа, а не списки: счётчики рядом со списками,
    # чтобы правило говорило «сколько», а отчёт человеку — «какие именно».
    for key in list(out):
        if isinstance(out[key], list):
            out[key + "Count"] = len(out[key])
    out["allWellFormed"] = not any(
        v for k, v in out.items() if isinstance(v, list))
    return out


def inspect(text, file_name=None, file_size=None, dictionary=None, level_grammar=None):
    """Вернуть плоский словарь фактов под пути evaluator-ов профиля."""
    doc = parse(text)
    ents = doc["entities"]
    header = doc["header"]
    index, psets = _property_index(ents)

    by_type = {}
    for eid, (etype, _a) in ents.items():
        by_type.setdefault(etype, []).append(eid)

    level_names = [
        _unquote(ents[e][1][2]) for e in by_type.get("IFCBUILDINGSTOREY", [])
        if len(ents[e][1]) > 2 and _unquote(ents[e][1][2])
    ]
    set_names = {name for name, _p in psets.values()}
    prop_names = {p for _n, props in psets.values() for p in props}

    facts = {
        "isStepPhysicalFile": True,
        "schema": header["schema"],
        "mvd": header["mvd"],
        "fileName": file_name,
        "fileSizeBytes": file_size,
        "inspectionVersion": INSPECTION_VERSION,
        "entityCount": len(ents),
        "buildingElementProxyCount": len(by_type.get("IFCBUILDINGELEMENTPROXY", [])),
        "lengthUnit": _length_unit(ents),
        "russetPropertySetCount": sum(1 for n in set_names if n.startswith("RusSet_")),
        "nonRussetPropertySetCount": sum(1 for n in set_names if not n.startswith("RusSet_")),
        "attributeNamesWithSpaces": sum(1 for n in set_names | prop_names if " " in n),
        "levelNames": level_names,
        "levelNamesWithBadChars": sum(1 for n in level_names if BAD_LEVEL_CHARS.search(n)),
    }

    # RUS_FNO ищется в параметрах проекта: он привязан к IfcBuilding (табл. Б.0)
    facts["project"] = {}
    for eid in by_type.get("IFCBUILDING", []):
        for (sname, pname), val in index.get(eid, {}).items():
            if pname == "RUS_FNO":
                facts["project"]["RUS_FNO"] = val

    facts["levelNaming"] = check_level_names(level_names, level_grammar)
    facts["attributes"] = _attribute_completeness(by_type, index, dictionary)
    # Классы, которых в файле нет, дают complete=None и в оценку не входят:
    # отсутствие класса не является нарушением, состав определяется проектом.
    judged = [v["complete"] for v in facts["attributes"].values() if v["complete"] is not None]
    facts["minAttributesPresent"] = all(judged) if judged else None
    return facts


def _attribute_completeness(by_type, index, dictionary):
    """По словарю: у каждого экземпляра класса есть все обязательные атрибуты."""
    out = {}
    if not dictionary:
        return out
    for table, cls in dictionary.get("classes", {}).items():
        ifc_class = cls["ifcClass"]
        required = {(a["set"], a["param"]) for a in cls["attributes"]
                    if a.get("set") and a.get("param")}
        ids = by_type.get(ifc_class.upper(), [])
        if not ids:
            # Класса в файле нет. Это не нарушение само по себе — состав
            # определяется проектом, — поэтому complete не выставляем.
            out[ifc_class] = {"table": table, "instances": 0, "complete": None,
                              "required": len(required)}
            continue
        missing = {}
        for eid in ids:
            have = set(index.get(eid, {}))
            gap = required - have
            if gap:
                missing[eid] = sorted(f"{s}.{p}" for s, p in gap)
        out[ifc_class] = {
            "table": table,
            "instances": len(ids),
            "required": len(required),
            "incompleteInstances": len(missing),
            "complete": not missing,
            "missingExample": next(iter(missing.values()), None),
        }
    return out
