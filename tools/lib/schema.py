"""Структурная валидация по JSON Schema — подмножество, без зависимостей.

Почему не библиотека `jsonschema`. Всё, что реально ловит ошибки в этом
репозитории — selfHash, висячие ссылки, циклы partOf, единственность active-профиля,
согласованность evaluator с verification — JSON Schema не выражает в принципе.
Зависимость купила бы проверку типов и enum'ов ценой второго механизма валидации
с собственным форматом ошибок и обязанностью держать значения синхронными в двух
местах. Схемы при этом нужны сами по себе: это публикуемый контракт для внешних
потребителей формата.

Поддерживаемое подмножество:
    type, required, enum, pattern, additionalProperties, properties,
    items, minItems, maxItems, $ref (только локальные #/$defs/...), oneOf
Ceiling: не поддерживаются allOf/anyOf/not, условные схемы, форматы, числовые
границы. Когда понадобится седьмая конструкция — стоит пересмотреть решение
в пользу библиотеки.
"""

import json
import re

TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value, expected):
    names = expected if isinstance(expected, list) else [expected]
    for name in names:
        py = TYPES.get(name)
        if py is None:
            continue
        # bool — подкласс int, но целым числом в схеме считаться не должен
        if name in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, py):
            return True
    return False


def _resolve(ref, root):
    if not ref.startswith("#/"):
        raise ValueError(f"поддерживаются только локальные $ref, получено {ref!r}")
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def validate(instance, schema, root=None, path="$"):
    """Вернуть список сообщений об ошибках. Пустой список — всё сходится."""
    root = root if root is not None else schema
    errors = []

    if "$ref" in schema:
        return validate(instance, _resolve(schema["$ref"], root), root, path)

    if "oneOf" in schema:
        for sub in schema["oneOf"]:
            if not validate(instance, sub, root, path):
                return []
        return [f"{path}: не подходит ни под один вариант oneOf"]

    if "type" in schema and not _type_ok(instance, schema["type"]):
        got = type(instance).__name__
        return [f"{path}: ожидался тип {schema['type']}, получен {got}"]

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: значение {instance!r} вне допустимых {schema['enum']}")

    if "pattern" in schema and isinstance(instance, str):
        if not re.search(schema["pattern"], instance):
            errors.append(f"{path}: {instance!r} не соответствует шаблону {schema['pattern']}")

    if isinstance(instance, dict):
        for field in schema.get("required", []):
            if field not in instance:
                errors.append(f"{path}: нет обязательного поля {field!r}")

        props = schema.get("properties", {})
        extra_schema = schema.get("additionalProperties")
        for key, value in instance.items():
            if key in props:
                errors += validate(value, props[key], root, f"{path}.{key}")
            elif extra_schema is False:
                errors.append(f"{path}: неизвестное поле {key!r}")
            elif isinstance(extra_schema, dict):
                errors += validate(value, extra_schema, root, f"{path}.{key}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: элементов {len(instance)}, минимум {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: элементов {len(instance)}, максимум {schema['maxItems']}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(instance):
                errors += validate(item, item_schema, root, f"{path}[{i}]")

    return errors


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def enum_of(schema, pointer):
    """Достать enum по пути вида '$defs/rule/properties/severity'.

    Нужно, чтобы validate.py не дублировал константы: схема остаётся единственным
    источником допустимых значений.
    """
    node = schema
    for part in pointer.split("/"):
        node = node[part]
    return set(node["enum"])
