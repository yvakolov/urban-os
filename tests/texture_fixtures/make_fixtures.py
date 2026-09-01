# -*- coding: utf-8 -*-
"""Генератор PNG-фикстур: минимальный корректный PNG заданных параметров.

Инспектор читает только чанк IHDR, но фикстура обязана быть настоящим PNG —
иначе тест проверял бы разбор мусора, а не разбор формата.
"""
import os
import struct
import zlib


def _chunk(tag, data):
    body = tag + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def png(path, width, height, colour_type=2, bit_depth=8):
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour_type]
    stride = width * channels * (bit_depth // 8)
    raw = b"".join(b"\x00" + b"\x80" * stride for _ in range(height))
    data = (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, bit_depth,
                                          colour_type, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw))
            + _chunk(b"IEND", b""))
    open(path, "wb").write(data)
    return len(data)


D = os.path.dirname(os.path.abspath(__file__))

if __name__ == "__main__":
    png(f"{D}/diffuse_1024.png", 1024, 1024)
    png(f"{D}/diffuse_2048.png", 2048, 2048)
    png(f"{D}/normal_512.png", 512, 512)
    png(f"{D}/with_alpha_512.png", 512, 512, colour_type=6)
    png(f"{D}/non_square_512x256.png", 512, 256)
    png(f"{D}/odd_size_300.png", 300, 300)
    png(f"{D}/sixteen_bit_512.png", 512, 512, bit_depth=16)
    open(f"{D}/not_a_png.png", "wb").write(b"GIF89a not a png")
    print("фикстур:", len([n for n in os.listdir(D) if n.endswith(".png")]))
