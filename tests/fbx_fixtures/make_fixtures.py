# -*- coding: utf-8 -*-
"""Генератор бинарных FBX-фикстур: пишет ровно тот формат, что читает инспектор.

Мелкие сцены лежат в репозитории рядом. Крупные — те, что должны перешагнуть
порог в 150 000 или 1 000 000 треугольников, — весят сотни килобайт и потому
не коммитятся: тест строит их этим же модулем во временном каталоге.

    python3 tests/fbx_fixtures/make_fixtures.py     # пересобрать мелкие
    from make_fixtures import scene                 # собрать любую
"""
import struct, zlib, os

MAGIC = b"Kaydara FBX Binary  \x00\x1a\x00"

def prop_i32(v): return b"I" + struct.pack("<i", v)
def prop_f64(v): return b"D" + struct.pack("<d", v)
def prop_i64(v): return b"L" + struct.pack("<q", v)
def prop_str(s):
    b = s.encode("utf-8"); return b"S" + struct.pack("<I", len(b)) + b
def prop_i32_array(vals, compress=False):
    payload = struct.pack("<%di" % len(vals), *vals)
    if compress:
        payload = zlib.compress(payload)
        return b"i" + struct.pack("<III", len(vals), 1, len(payload)) + payload
    return b"i" + struct.pack("<III", len(vals), 0, len(payload)) + payload

WIDE = [False]   # версия >= 7500 -> 64-битные смещения в записях узлов

def node(name, props=(), children=(), offset=0):
    """Собрать запись узла, зная своё смещение в файле (нужен EndOffset)."""
    nb = name.encode("utf-8")
    prop_bytes = b"".join(props)
    fmt, null_size = ("<QQQB", 25) if WIDE[0] else ("<IIIB", 13)
    header_size = null_size + len(nb)
    body = b""
    pos = offset + header_size + len(prop_bytes)
    for c in children:
        chunk = c(pos)
        body += chunk
        pos += len(chunk)
    if children:
        body += b"\x00" * null_size
        pos += null_size
    return (struct.pack(fmt, pos, len(props), len(prop_bytes), len(nb))
            + nb + prop_bytes + body)

def N(name, props=(), children=()):
    return lambda off: node(name, props, children, off)

def build(version, roots):
    WIDE[0] = version >= 7500
    out = MAGIC + struct.pack("<I", version)
    pos = len(out)
    for r in roots:
        chunk = r(pos); out += chunk; pos += len(chunk)
    return out + b"\x00" * (25 if WIDE[0] else 13)

def geometry(uid, name, tri_count, triangulated=True):
    idx = []
    if triangulated:
        for i in range(tri_count):
            a = i * 3
            idx += [a, a + 1, ~(a + 2)]
    else:
        # четырёхугольники: каждый даёт 2 треугольника
        for i in range(tri_count // 2):
            a = i * 4
            idx += [a, a + 1, a + 2, ~(a + 3)]
    return N("Geometry", (prop_i64(uid), prop_str(f"Geometry::{name}"), prop_str("Mesh")),
             (N("PolygonVertexIndex", (prop_i32_array(idx, compress=len(idx) > 60),)),))

def model(uid, name, kind):
    return N("Model", (prop_i64(uid), prop_str(f"Model::{name}"), prop_str(kind)))

def material(uid, name):
    return N("Material", (prop_i64(uid), prop_str(f"Material::{name}"), prop_str("")))

def video(uid, name, embedded=False):
    kids = (N("Content", (b"R" + struct.pack("<I", 4) + b"\x89PNG",)),) if embedded else ()
    return N("Video", (prop_i64(uid), prop_str(f"Video::{name}"), prop_str("Clip")), kids)

def settings(unit_scale):
    return N("GlobalSettings", (), (
        N("Properties70", (), (
            N("P", (prop_str("UnitScaleFactor"), prop_str("double"),
                    prop_str("Number"), prop_str(""), prop_f64(unit_scale))),
        )),
    ))

def scene(path, version=7400, unit_scale=100.0, tris=12, triangulated=True,
          materials=2, cameras=0, lights=0, bones=0, nulls=0, anim=0,
          embedded_textures=0, geometries=1):
    objs = []
    uid = 1000
    for g in range(geometries):
        objs.append(geometry(uid, f"mesh{g}", tris // geometries, triangulated)); uid += 1
        objs.append(model(uid, f"Main_{g}", "Mesh")); uid += 1
    for i in range(materials): objs.append(material(uid, f"mat{i}")); uid += 1
    for i in range(cameras): objs.append(model(uid, f"cam{i}", "Camera")); uid += 1
    for i in range(lights): objs.append(model(uid, f"light{i}", "Light")); uid += 1
    for i in range(bones): objs.append(model(uid, f"bone{i}", "LimbNode")); uid += 1
    for i in range(nulls): objs.append(model(uid, f"null{i}", "Null")); uid += 1
    for i in range(anim):
        objs.append(N("AnimationStack", (prop_i64(uid), prop_str("AnimStack::Take"), prop_str("")))); uid += 1
    for i in range(embedded_textures): objs.append(video(uid, f"tex{i}", True)); uid += 1

    WIDE[0] = version >= 7500
    roots = [
        N("FBXHeaderExtension", (), (N("FBXVersion", (prop_i32(version),)),)),
        settings(unit_scale),
        N("Objects", (), tuple(objs)),
        N("Connections", ()),
    ]
    open(path, "wb").write(build(version, roots))
    return os.path.getsize(path)

D = os.path.dirname(os.path.abspath(__file__))


def _small():
    scene(f"{D}/npm-oks-valid.fbx", tris=1200, materials=3)
    scene(f"{D}/vpm-oks-valid.fbx", tris=5000, materials=5)
    scene(f"{D}/scene-not-clean.fbx", tris=100, cameras=1, lights=2, bones=3, nulls=2, anim=1)
    scene(f"{D}/quads-not-triangulated.fbx", tris=100, triangulated=False)
    scene(f"{D}/centimetre-scale.fbx", tris=50, unit_scale=1.0)
    scene(f"{D}/fbx-7500-wide.fbx", version=7500, tris=30)
    scene(f"{D}/embedded-textures.fbx", tris=50, embedded_textures=2)
    scene(f"{D}/too-many-materials.fbx", tris=50, materials=9)
    # текстовый FBX: требования предписывают binary, и принимать ASCII нельзя
    open(f"{D}/not-fbx.fbx", "wb").write(b"; FBX 7.4.0 project file\nObjects:  {\n}\n")


if __name__ == "__main__":
    _small()
    print("фикстур:", len([n for n in os.listdir(D) if n.endswith(".fbx")]))
