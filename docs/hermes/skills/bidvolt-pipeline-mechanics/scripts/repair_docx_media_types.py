#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复 docx 媒体类型与内容不一致（图片无法显示的根因修复，项目 217 实测）。

事故：压缩脚本把 PNG 重编码为 JPEG，但媒体部件名无扩展名（如 word/media/imageN.）
导致重命名正则失效，[Content_Types].xml 仍声明 image/png → Word 按声明类型解码失败、图片无法显示。

本脚本按媒体字节魔数探测真实格式，把 [Content_Types].xml 的 Override 修正为一致类型；
不改媒体文件名、不改 rels（引用一致），最小化改动。

用法：python3 repair_docx_media_types.py a.docx b.docx ...
"""

from __future__ import annotations

import io
import re
import sys
import zipfile

_MAGIC = [
    (b"\x89PNG", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
]


def _sniff(data: bytes) -> str | None:
    for magic, ctype in _MAGIC:
        if data.startswith(magic):
            return ctype
    return None


def repair(data: bytes) -> tuple[bytes, int]:
    zin = zipfile.ZipFile(io.BytesIO(data))
    infos = zin.infolist()
    cts_name = next(
        (n for n in zin.namelist() if n.lower().endswith("[content_types].xml")),
        "[Content_Types].xml",
    )
    cts = zin.read(cts_name).decode("utf-8")
    fixed = 0

    def _repl(m: re.Match) -> str:
        nonlocal fixed
        part, decl = m.group(1), m.group(2)
        entry = "word" + part.split("/word", 1)[1]
        try:
            actual = _sniff(zin.read(entry))
        except KeyError:
            return m.group(0)
        if actual and actual != decl:
            fixed += 1
            return f'<Override PartName="{part}" ContentType="{actual}"/>'
        return m.group(0)

    new_cts = re.sub(
        r'<Override PartName="(/word/media/[^"]+)" ContentType="(image/[^"]+)"/>',
        _repl,
        cts,
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for it in infos:
            payload = (
                new_cts.encode("utf-8")
                if it.filename == cts_name
                else zin.read(it.filename)
            )
            zout.writestr(it, payload)
    return out.getvalue(), fixed


def main() -> None:
    if len(sys.argv) < 2:
        print("用法：python3 repair_docx_media_types.py a.docx b.docx ...")
        sys.exit(1)
    for path in sys.argv[1:]:
        data = open(path, "rb").read()
        new, fixed = repair(data)
        if fixed:
            open(path, "wb").write(new)
            print(f"{path}: fixed {fixed} media content types")
        else:
            print(f"{path}: no mismatch")


if __name__ == "__main__":
    main()
