"""Извлечение содержательной части из ответа официального источника.

Стратегия window_data_json: страница услуги на mos.ru — React SPA, но весь контент
лежит в исходном HTML как `window.DATA = {...}` плюс `window.TARGET_ID = "..."`.
Исполнять JS не нужно.

Позитивные инварианты здесь важнее самого извлечения: страница может ответить 200 с
заглушкой WAF, капчей или карточкой другой услуги. Такой ответ обязан стать
PARSER_ERROR, а не CHANGED, иначе система откроет issue «изменился НПА» на пустом
месте.
"""

import json
import re

EXTRACTION_VERSION = 1

TARGET_ID_RE = re.compile(r'window\.TARGET_ID\s*=\s*"([^"]+)"')
DATA_MARKER = "window.DATA"


class ExtractionError(Exception):
    """Содержательную часть достать не удалось — ответ не тот, что мы ждём."""


def _find_data_object(html):
    """Вернуть dict из window.DATA.

    Используем json.JSONDecoder().raw_decode — он корректно останавливается на
    границе объекта, включая строки со скобками, экранированными кавычками и
    последовательностью </script>. Собственная балансировка скобок здесь не нужна
    и на таких строках ошибается.
    """
    pos = html.find(DATA_MARKER)
    if pos < 0:
        raise ExtractionError("в ответе нет window.DATA")
    brace = html.find("{", pos)
    if brace < 0:
        raise ExtractionError("после window.DATA нет объекта")
    try:
        obj, _end = json.JSONDecoder().raw_decode(html, brace)
    except ValueError as exc:
        raise ExtractionError(f"window.DATA не разбирается как JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ExtractionError(f"window.DATA не объект, а {type(obj).__name__}")
    return obj


def _canonical_size(obj):
    """Размер в канонической сериализации, а не длина исходного фрагмента.

    Длина сырого текста зависит от экранирования: mos.ru отдаёт кириллицу как
    \\uXXXX, и тот же самый объект, записанный литералами, втрое короче. Коридор
    размера, построенный на сыром тексте, ловил бы смену представления как
    подмену страницы.
    """
    return len(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def extract(html, invariants=None):
    """html -> {'data', 'targetId', 'payloadBytes', 'extractionVersion'}.

    invariants (все необязательны):
      targetId         ожидаемый window.TARGET_ID
      requiredBranches ветки, которые обязаны присутствовать в data['blocks']
      sizeCorridor     [min, max] множители относительно baselinePayloadBytes
      baselinePayloadBytes  размер фрагмента window.DATA в утверждённом baseline
    """
    inv = invariants or {}

    m = TARGET_ID_RE.search(html)
    target_id = m.group(1) if m else None

    expected = inv.get("targetId")
    if expected is not None and target_id != expected:
        raise ExtractionError(
            f"window.TARGET_ID = {target_id!r}, ожидался {expected!r} — "
            "отдана другая страница"
        )

    data = _find_data_object(html)
    payload_bytes = _canonical_size(data)

    blocks = data.get("blocks")
    if not isinstance(blocks, dict):
        raise ExtractionError("в window.DATA нет объекта blocks")

    missing = [b for b in inv.get("requiredBranches", []) if b not in blocks]
    if missing:
        raise ExtractionError(
            "в blocks отсутствуют обязательные ветки: " + ", ".join(sorted(missing))
        )

    corridor = inv.get("sizeCorridor")
    baseline_bytes = inv.get("baselinePayloadBytes")
    if corridor and baseline_bytes:
        low, high = float(corridor[0]), float(corridor[1])
        ratio = payload_bytes / baseline_bytes
        if not low <= ratio <= high:
            raise ExtractionError(
                f"размер window.DATA {payload_bytes} Б — это ×{ratio:.2f} от baseline "
                f"({baseline_bytes} Б), коридор ×{low}–×{high}"
            )

    return {
        "data": data,
        "targetId": target_id,
        "payloadBytes": payload_bytes,
        "extractionVersion": EXTRACTION_VERSION,
    }
