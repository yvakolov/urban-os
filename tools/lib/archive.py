"""Инспектор ZIP-архивов НПМ и ВПМ.

Требование одно и то же в обоих корпусах: центральная директория и локальные
заголовки читаются, шифрования нет, опасных путей нет, дублей имён нет,
сжатие поддерживается. Всё это меряется стандартным zipfile.

Опасный путь — это абсолютный путь либо выход за пределы архива через «..».
Проверка нужна не ради формальности: распаковка такого архива на стороне
ведомства пишет файлы мимо каталога назначения.
"""

import os
import posixpath
import zipfile

INSPECTION_VERSION = 1

# п. «неподдерживаемое сжатие»: store и deflate. Остальное ведомственный
# распаковщик может не открыть, и архив станет нечитаемым уже у них.
SUPPORTED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


def _dangerous(name):
    n = name.replace("\\", "/")
    if n.startswith("/") or (len(n) > 1 and n[1] == ":"):
        return True
    return any(part == ".." for part in posixpath.normpath(n).split("/"))


def inspect(path):
    """Путь к ZIP -> факты под evaluator-ы. Нечитаемый архив — факт, не исключение."""
    facts = {
        "inspectionVersion": INSPECTION_VERSION,
        "fileSizeBytes": os.path.getsize(path) if os.path.exists(path) else None,
        "readable": False,
        "encrypted": False,
        "dangerousPaths": [],
        "duplicateNames": [],
        "unsupportedCompression": [],
        "corruptEntries": [],
        "error": None,
    }
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            facts["readable"] = True
            facts["entryCount"] = len(infos)
            names = [i.filename for i in infos]
            seen, dupes = set(), set()
            for n in names:
                (dupes if n in seen else seen).add(n)
            facts["duplicateNames"] = sorted(dupes)
            facts["dangerousPaths"] = sorted(n for n in names if _dangerous(n))
            # бит 0 общего флага — файл зашифрован
            facts["encrypted"] = any(i.flag_bits & 0x1 for i in infos)
            facts["unsupportedCompression"] = sorted(
                {i.compress_type for i in infos} - SUPPORTED_COMPRESSION)
            facts["uncompressedBytes"] = sum(i.file_size for i in infos)
            if not facts["encrypted"]:
                # testzip читает каждый локальный заголовок и сверяет CRC —
                # ровно то, что требует «локальные заголовки читаются».
                bad = z.testzip()
                if bad:
                    facts["corruptEntries"] = [bad]
    except (zipfile.BadZipFile, OSError, NotImplementedError) as exc:
        facts["error"] = f"{type(exc).__name__}: {exc}"

    facts["valid"] = (
        facts["readable"]
        and not facts["encrypted"]
        and not facts["dangerousPaths"]
        and not facts["duplicateNames"]
        and not facts["unsupportedCompression"]
        and not facts["corruptEntries"]
    )
    return facts


def names(path):
    """Имена записей архива — для проверок состава пакета."""
    try:
        with zipfile.ZipFile(path) as z:
            return [i.filename for i in z.infolist() if not i.is_dir()]
    except (zipfile.BadZipFile, OSError):
        return []
