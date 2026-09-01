"""Детерминированная нормализация содержательной части и её хеш.

Разведка показала, что mos.ru отдаёт побайтово идентичный HTML между запросами:
волатильных полей (session id, timestamps) нет. Поэтому агрессивная нормализация
не нужна и вредна — она способна спрятать юридически значимое изменение.

Делаем ровно две вещи:
  1) берём только те ветки blocks, что перечислены в includeBranches;
  2) сериализуем канонически (sort_keys, без пробелов) и хешируем.

Массивы НЕ сортируем: порядок пунктов в перечне документов и в основаниях для
отказа семантичен, перестановка — значимое изменение, а не шум.

excludeKeys вырезаются рекурсивно по имени ключа. Туда попадает updatedAt: сегодня
это заглушка 0001-01-01T00:00:00Z, но если её однажды заполнят, поле начнёт
фонить при каждой правке страницы.
"""

import hashlib
import json

NORMALIZATION_VERSION = 1

# Отличаем «ветки нет» от «ветка пустая»: без этого исчезновение целого раздела
# читалось бы как отсутствие изменений.
MISSING = "__branch_absent__"


def _strip_keys(value, exclude):
    if isinstance(value, dict):
        return {k: _strip_keys(v, exclude) for k, v in value.items() if k not in exclude}
    if isinstance(value, list):
        return [_strip_keys(v, exclude) for v in value]
    return value


def normalize(data, spec=None):
    """data (window.DATA) -> canonical dict пригодный для сравнения и диффа."""
    spec = spec or {}
    include = spec.get("includeBranches")
    exclude = set(spec.get("excludeKeys", []))

    blocks = data.get("blocks", {})
    if include is None:
        include = sorted(blocks.keys())

    out = {}
    for branch in include:
        out[branch] = MISSING if branch not in blocks else _strip_keys(blocks[branch], exclude)
    return out


def canonical(value):
    """Каноническая сериализация: одна строка, ключи отсортированы, без пробелов."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def branch_hashes(normalized):
    """Хеш каждой ветки отдельно — по ним определяем, что именно изменилось."""
    return {branch: content_hash(value) for branch, value in normalized.items()}


def changed_branches(before, after):
    """Список веток, чьи хеши разошлись. Включает появившиеся и исчезнувшие."""
    hb, ha = branch_hashes(before), branch_hashes(after)
    return sorted(b for b in set(hb) | set(ha) if hb.get(b) != ha.get(b))


def branch_diff(before, after, branch, max_lines=40):
    """Человекочитаемый diff одной ветки. Построчно по каноническому JSON с отступами."""
    import difflib

    def lines(src):
        if branch not in src:
            return ["<ветки нет>"]
        return json.dumps(
            src[branch], ensure_ascii=False, sort_keys=True, indent=2
        ).splitlines()

    diff = list(
        difflib.unified_diff(
            lines(before), lines(after), fromfile="baseline", tofile="current", lineterm="", n=2
        )
    )
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"... ещё {len(diff) - max_lines} строк"]
    return "\n".join(diff)
