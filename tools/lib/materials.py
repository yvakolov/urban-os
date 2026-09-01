"""Инспектор материалов АГР: один PDF, его состав и ведомость ТЭП.

Меряет то, что задаёт приложение 1 к Административному регламенту: состав
разделов, наличие закладок под них, точность координат, полноту ведомости ТЭП.
Правил не знает — какие разделы применимы к конкретному объекту, решают
appliesWhen в профиле.

Работает с манифестом, а не с самим PDF. Причина не в лени: разрешение
изображения в PDF задаётся матрицей размещения на странице, а состав разделов —
смыслом содержимого, и ни то, ни другое не извлекается из байтов надёжно.
Манифест выдаёт инструмент, который PDF собирал, и он знает, что в него положил.

Манифест:
    {"pdf": {"files": ["АГР.pdf"], "encrypted": false,
             "sections": ["1.3", "1.4", ...],      # разделы, вошедшие в файл
             "bookmarks": ["1.3", "1.4", ...]},    # разделы, на которые есть закладка
     "coordinates": {"system": "МСК77",
                     "points": [{"x": "12345.67", "y": "9876.54"}]},
     "tep": {"built-up-area": 1234.5, ...}}
"""

INSPECTION_VERSION = 1

# п. 2.1.4: точность записи координат зависит от системы
COORD_PRECISION = {"WGS84": 7, "МСК77": 2}

# п. 2.1.11 — перечень показателей ведомости ТЭП. Ключи совпадают с хвостами
# идентификаторов правил mat-2-1-11-*, чтобы связь правила и факта была видна
# без отдельной таблицы соответствия.
TEP_INDICATORS = (
    "built-up-area", "total-area", "above-ground-area", "underground-area",
    "floors", "absolute-height", "height", "total-floor-area", "parking",
)

# п. 2.1.11(1): для транспортных сооружений перечень сокращён
TEP_TRANSPORT = ("built-up-area", "height", "absolute-height")


def _decimals(value):
    """Знаков после запятой в записи координаты.

    Значение берётся строкой намеренно: 55.7500000 и 55.75 равны как числа, но
    как записи координаты различаются, а требование предъявлено к записи.
    """
    s = str(value).strip().replace(",", ".")
    return len(s.split(".", 1)[1]) if "." in s else 0


def inspect(manifest, composition_items=None):
    """manifest (+ перечень разделов состава) -> плоские факты под evaluator-ы."""
    pdf = manifest.get("pdf") or {}
    files = list(pdf.get("files") or [])
    sections = set(pdf.get("sections") or [])
    bookmarks = set(pdf.get("bookmarks") or [])

    unbookmarked = sorted(sections - bookmarks)
    facts = {
        "inspectionVersion": INSPECTION_VERSION,
        "pdf": {
            "fileCount": len(files),
            "encrypted": bool(pdf.get("encrypted")),
            # п. 2.1: один файл, в формате PDF, без защиты паролем
            "singleUnprotectedFile": len(files) == 1
            and files[0].lower().endswith(".pdf")
            and not pdf.get("encrypted"),
            "sectionCount": len(sections),
            "bookmarkCount": len(bookmarks),
            "unbookmarked": unbookmarked,
            "unbookmarkedCount": len(unbookmarked),
            "bookmarksCoverComposition": not unbookmarked and bool(sections),
            # Закладка на раздел, которого в файле нет, — не нарушение, а признак
            # рассогласования: сообщаем, но в вердикт не превращаем.
            "bookmarksWithoutSection": sorted(bookmarks - sections),
        },
        "composition": {
            num.replace(".", "-"): {"number": num, "present": num in sections}
            for num in (composition_items or sorted(sections))
        },
    }

    coords = manifest.get("coordinates") or {}
    if coords.get("points"):
        system = coords.get("system")
        need = COORD_PRECISION.get(system)
        digits = [min(_decimals(p.get("x")), _decimals(p.get("y")))
                  for p in coords["points"]]
        facts["coordinates"] = {
            "system": system,
            "pointCount": len(digits),
            "minDecimals": min(digits),
            "required": need,
            # Система координат не из списка — не «не соответствует», а
            # «не определено»: п. 2.1.4 допускает только WGS84 и МСК77, и
            # чужая система это отдельное нарушение, а не низкая точность.
            "precisionOk": None if need is None else min(digits) >= need,
            "systemAllowed": system in COORD_PRECISION,
        }

    tep = manifest.get("tep") or {}
    facts["tep"] = {k: {"present": tep.get(k) is not None} for k in TEP_INDICATORS}
    facts["tep"]["declaredCount"] = sum(
        1 for k in TEP_INDICATORS if tep.get(k) is not None)
    facts["tep"]["missing"] = sorted(k for k in TEP_INDICATORS if tep.get(k) is None)
    return facts
