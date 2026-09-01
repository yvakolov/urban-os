"""Инспектор FBX binary: разбор дерева узлов и сбор фактов о сцене.

Приёмочная модель НПМ и ВПМ подаётся в FBX, а не в GLB. До сих пор машинные
проверки в профилях смотрели на glb.* — то есть на производный просмотрочный
файл, а не на то, что принимает ведомство. Этот инспектор читает сам FBX.

Как и остальные инспекторы, правил не знает: пороги треугольников, лимиты
материалов и допустимые версии живут в requirements/.

Почему свой парсер. Нужен разбор ровно бинарного FBX: заголовок, узлы,
свойства, массивы. Это около двухсот строк на стандартной библиотеке, включая
zlib для сжатых массивов. Готовые библиотеки тянут за собой геометрические ядра
и бинарные колёса под каждую платформу, а инструмент должен запускаться везде,
где есть python3.

Ceiling: разбирается бинарный FBX. Текстовый (ASCII) FBX не поддерживается —
требования предписывают binary, и попытка угадать текстовый формат означала бы
принимать то, что подавать нельзя. Такой файл даёт FbxError.
"""

import struct
import zlib

INSPECTION_VERSION = 1

MAGIC = b"Kaydara FBX Binary  \x00"
# Начиная с версии 7500 смещения в записях узлов 64-битные
WIDE_VERSION = 7500

ARRAY_TYPES = {"f": ("f", 4), "d": ("d", 8), "l": ("q", 8), "i": ("i", 4), "b": ("b", 1)}
SCALAR_TYPES = {"Y": ("h", 2), "C": ("?", 1), "I": ("i", 4),
                "F": ("f", 4), "D": ("d", 8), "L": ("q", 8)}


class FbxError(Exception):
    """Файл не разбирается как бинарный FBX."""


class Node:
    __slots__ = ("name", "props", "children")

    def __init__(self, name, props, children):
        self.name = name
        self.props = props
        self.children = children

    def find(self, name):
        return [c for c in self.children if c.name == name]

    def first(self, name):
        for c in self.children:
            if c.name == name:
                return c
        return None


# ---------------------------------------------------------------- разбор

def _read_property(buf, pos):
    kind = chr(buf[pos]); pos += 1
    if kind in SCALAR_TYPES:
        fmt, size = SCALAR_TYPES[kind]
        (value,) = struct.unpack_from("<" + fmt, buf, pos)
        return value, pos + size
    if kind in ARRAY_TYPES:
        fmt, size = ARRAY_TYPES[kind]
        length, encoding, comp_len = struct.unpack_from("<III", buf, pos)
        pos += 12
        raw = buf[pos:pos + comp_len]
        pos += comp_len
        if encoding == 1:
            raw = zlib.decompress(raw)
        elif encoding != 0:
            raise FbxError(f"неизвестная кодировка массива: {encoding}")
        if len(raw) < length * size:
            raise FbxError("массив короче объявленной длины")
        return list(struct.unpack_from("<%d%s" % (length, fmt), raw, 0)), pos
    if kind in ("S", "R"):
        (length,) = struct.unpack_from("<I", buf, pos)
        pos += 4
        raw = bytes(buf[pos:pos + length])
        pos += length
        if kind == "R":
            return raw, pos
        # FBX разделяет составные имена парой \x00\x01 — для нас это один текст
        return raw.replace(b"\x00\x01", b"::").decode("utf-8", "replace"), pos
    raise FbxError(f"неизвестный тип свойства: {kind!r}")


def _read_node(buf, pos, wide):
    hdr = "<QQQB" if wide else "<IIIB"
    hdr_size = 25 if wide else 13
    if pos + hdr_size > len(buf):
        raise FbxError("запись узла выходит за пределы файла")
    end_offset, num_props, prop_len, name_len = struct.unpack_from(hdr, buf, pos)
    pos += hdr_size
    if end_offset == 0:
        return None, pos  # нулевая запись — конец списка узлов
    name = bytes(buf[pos:pos + name_len]).decode("utf-8", "replace")
    pos += name_len

    props = []
    prop_end = pos + prop_len
    for _ in range(num_props):
        value, pos = _read_property(buf, pos)
        props.append(value)
    pos = prop_end

    children = []
    while pos < end_offset:
        child, pos = _read_node(buf, pos, wide)
        if child is None:
            break
        children.append(child)
    return Node(name, props, children), end_offset


def parse(raw):
    """bytes -> (version, [корневые узлы])."""
    if not raw.startswith(MAGIC):
        raise FbxError("нет сигнатуры Kaydara FBX Binary — это не бинарный FBX")
    (version,) = struct.unpack_from("<I", raw, 23)
    wide = version >= WIDE_VERSION
    pos = 27
    roots = []
    while pos < len(raw) - (25 if wide else 13):
        node, pos = _read_node(raw, pos, wide)
        if node is None:
            break
        roots.append(node)
    if not roots:
        raise FbxError("в файле нет ни одного узла")
    return version, roots


# ---------------------------------------------------------------- факты

def _property70(node):
    """Properties70 узла как словарь имя -> значение."""
    out = {}
    p70 = node.first("Properties70") if node else None
    if not p70:
        return out
    for p in p70.find("P"):
        if p.props:
            # P: [имя, тип, подтип, флаги, значение...]
            out[p.props[0]] = p.props[4] if len(p.props) > 4 else None
    return out


def _polygons(index_array):
    """Полигоны и треугольники по PolygonVertexIndex.

    В FBX последний индекс каждого полигона записан как ~i (побитовое НЕ),
    поэтому границы полигонов читаются из самого массива. Треугольников в
    полигоне из n вершин — n-2.
    """
    polys, tris, size = 0, 0, 0
    non_triangles = 0
    for i in index_array:
        size += 1
        if i < 0:
            polys += 1
            if size >= 3:
                tris += size - 2
            if size != 3:
                non_triangles += 1
            size = 0
    return polys, tris, non_triangles


def inspect(raw, file_name=None):
    """raw bytes FBX -> плоские факты под пути evaluator-ов профиля."""
    version, roots = parse(raw)
    by_name = {}
    for r in roots:
        by_name.setdefault(r.name, []).append(r)

    objects = by_name.get("Objects", [None])[0]
    kinds = {}
    names = {"Model": [], "Material": [], "Texture": [], "Video": []}
    triangles = 0
    polygons = 0
    non_triangulated_polygons = 0
    geometries = 0
    model_types = {}
    if objects:
        for child in objects.children:
            kinds[child.name] = kinds.get(child.name, 0) + 1
            if child.name in names and len(child.props) >= 2:
                # «Model::Main_1» -> «Main_1»: маски именования в требованиях
                # предъявлены к имени объекта, а не к префиксу типа
                label = child.props[1]
                names[child.name].append(
                    label.split("::", 1)[1] if "::" in label else label)
            if child.name == "Model" and len(child.props) >= 3:
                t = child.props[2]
                model_types[t] = model_types.get(t, 0) + 1
            if child.name == "Geometry":
                geometries += 1
                idx = child.first("PolygonVertexIndex")
                if idx and idx.props and isinstance(idx.props[0], list):
                    p, t, nt = _polygons(idx.props[0])
                    polygons += p
                    triangles += t
                    non_triangulated_polygons += nt

    settings = by_name.get("GlobalSettings", [None])[0]
    gprops = _property70(settings)

    facts = {
        "isBinaryFbx": True,
        "inspectionVersion": INSPECTION_VERSION,
        "version": version,
        "fileName": file_name,
        "fileSizeBytes": len(raw),
        "geometries": geometries,
        "polygons": polygons,
        "triangles": triangles,
        "nonTriangulatedPolygons": non_triangulated_polygons,
        "triangulated": non_triangulated_polygons == 0 and polygons > 0,
        "materials": kinds.get("Material", 0),
        "textures": kinds.get("Texture", 0),
        "videos": kinds.get("Video", 0),
        "deformers": kinds.get("Deformer", 0),
        "poses": kinds.get("Pose", 0),
        # Анимация в FBX живёт в стеках и слоях; наличие стека и означает её
        "animations": kinds.get("AnimationStack", 0),
        "animationCurves": kinds.get("AnimationCurveNode", 0),
        "meshes": model_types.get("Mesh", 0),
        "cameras": model_types.get("Camera", 0),
        "lights": model_types.get("Light", 0),
        "nulls": model_types.get("Null", 0),
        "bones": model_types.get("LimbNode", 0),
        "modelTypes": model_types,
        "modelNames": names["Model"],
        "materialNames": names["Material"],
        "textureNames": names["Texture"] + names["Video"],
        "unitScaleFactor": gprops.get("UnitScaleFactor"),
        "originalUnitScaleFactor": gprops.get("OriginalUnitScaleFactor"),
    }
    # FBX хранит масштаб в сантиметрах: 1.0 — сантиметр, 100.0 — метр.
    # Требование «1 единица равна 1 метру» — это UnitScaleFactor 100.
    usf = facts["unitScaleFactor"]
    facts["metreScale"] = None if usf is None else abs(usf - 100.0) < 1e-6
    # Встроенные текстуры: Video с содержимым Content нарушают «текстуры
    # предоставлены отдельными PNG»
    embedded = 0
    if objects:
        for child in objects.children:
            if child.name == "Video":
                content = child.first("Content")
                if content and content.props and content.props[0]:
                    embedded += 1
    facts["embeddedTextures"] = embedded

    # Сцена без запрещённых сущностей. Значение трёхзначное намеренно: люди,
    # транспорт, коммуникации и «лишние слои» из требований по байтам файла не
    # опознаются. Машина умеет ОПРОВЕРГНУТЬ чистоту сцены, но не подтвердить,
    # поэтому True здесь не возникает никогда — подтверждает человек.
    forbidden = (facts["cameras"] + facts["animations"] + facts["animationCurves"]
                 + facts["bones"] + facts["nulls"] + facts["deformers"] + facts["poses"])
    facts["sceneClean"] = False if forbidden else None
    facts["forbiddenEntityCount"] = forbidden
    return facts
