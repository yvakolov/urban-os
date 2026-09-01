"""Инспектор PNG-текстур: размеры, глубина, альфа-канал, вес.

Требования к текстурам числовые и потому проверяются точно: квадрат из
разрешённого набора размеров, 8 бит на канал, наличие или отсутствие альфы,
предельный вес файла. Всё это лежит в чанке IHDR — первых 33 байтах файла,
и разбирается стандартной библиотекой.

Ceiling: содержимое пикселей не читается. Плотность текселя, padding между
UV-островами и «выраженность фактуры» отсюда не следуют — это остаётся за
человеком.
"""

import os
import struct

INSPECTION_VERSION = 1

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# IHDR, colour type -> число каналов и наличие альфы
COLOUR_TYPES = {
    0: ("grayscale", 1, False),
    2: ("rgb", 3, False),
    3: ("indexed", 1, False),
    4: ("grayscale_alpha", 2, True),
    6: ("rgba", 4, True),
}


def read(path):
    """Путь к PNG -> факты об одном файле. Не-PNG — факт, а не исключение."""
    facts = {
        "name": os.path.basename(path),
        "sizeBytes": os.path.getsize(path) if os.path.exists(path) else None,
        "isPng": False,
    }
    try:
        with open(path, "rb") as fh:
            head = fh.read(33)
    except OSError as exc:
        facts["error"] = str(exc)
        return facts
    if not head.startswith(PNG_MAGIC) or head[12:16] != b"IHDR":
        return facts
    width, height, depth, colour = struct.unpack_from(">IIBB", head, 16)
    kind, channels, has_alpha = COLOUR_TYPES.get(colour, ("unknown", None, None))
    facts.update({
        "isPng": True,
        "width": width,
        "height": height,
        "square": width == height,
        "bitDepth": depth,
        "colourType": kind,
        "channels": channels,
        "hasAlpha": has_alpha,
    })
    return facts


def inspect(paths, allowed_sizes=None, max_bytes=None, require_no_alpha=False):
    """Набор текстур -> сводные факты под evaluator-ы."""
    files = [read(p) for p in paths]
    pngs = [f for f in files if f["isPng"]]
    facts = {
        "inspectionVersion": INSPECTION_VERSION,
        "count": len(files),
        "notPng": sorted(f["name"] for f in files if not f["isPng"]),
        "files": files,
    }
    if not pngs:
        return facts
    facts["maxDimension"] = max(max(f["width"], f["height"]) for f in pngs)
    facts["nonSquare"] = sorted(f["name"] for f in pngs if not f["square"])
    facts["withAlpha"] = sorted(f["name"] for f in pngs if f["hasAlpha"])
    facts["notEightBit"] = sorted(f["name"] for f in pngs if f["bitDepth"] != 8)
    if allowed_sizes:
        allowed = set(allowed_sizes)
        facts["wrongSize"] = sorted(
            f["name"] for f in pngs
            if not (f["square"] and f["width"] in allowed))
        facts["sizesValid"] = not facts["wrongSize"] and not facts["notPng"]
    if max_bytes:
        facts["oversize"] = sorted(f["name"] for f in files
                                   if (f["sizeBytes"] or 0) > max_bytes)
        facts["sizeOk"] = not facts["oversize"]
    if require_no_alpha:
        facts["alphaFree"] = not facts["withAlpha"] and not facts["notPng"]
    return facts
