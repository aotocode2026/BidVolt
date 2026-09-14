#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复 docx 媒体类型与内容不一致 + 内容类型表悬空声明（两次 217 事故的根因修复）。

事故一（图片不显示）：压缩脚本把 PNG 重编码为 JPEG，但媒体部件名无扩展名（如 word/media/imageN.）
导致重命名正则失效，[Content_Types].xml 仍声明 image/png → Word 按声明类型解码失败、图片无法显示。

事故二（Word 报「文件已损坏」，issue #69）：压缩脚本把媒体部件 `imageN.` 重命名成 `imageN.jpeg`
并同步了 rels，却只补了 `Default Extension="jpeg"`、**没删掉原先按部件名写的 402 条 Override**
→ 内容类型表里指向不存在部件，Word 判定包结构不一致而拒绝打开（LibreOffice/python-docx 只查
实际存在的部件，所以平台渲染一路绿灯）。

本脚本做两件事：①按媒体字节魔数探测真实格式，把 [Content_Types].xml 的 Override 修正为一致类型；
②删掉指向不存在部件的 Override。不改媒体文件名、不改 rels（引用一致），最小化改动。

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


def repair(data: bytes) -> tuple[bytes, dict]:
    zin = zipfile.ZipFile(io.BytesIO(data))
    infos = zin.infolist()
    names = {i.filename for i in infos}
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
    # ② 删掉指向不存在部件的 Override（issue #69：Word 判"文件损坏"）
    stale = 0

    def _drop_stale(m: re.Match) -> str:
        nonlocal stale
        tag = m.group(0)
        pm = re.search(r'PartName="([^"]+)"', tag)
        if pm and pm.group(1).lstrip("/") in names:
            return tag
        stale += 1
        return ""

    new_cts = re.sub(r"<Override\b[^>]*/>", _drop_stale, new_cts)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for it in infos:
            payload = (
                new_cts.encode("utf-8")
                if it.filename == cts_name
                else zin.read(it.filename)
            )
            zout.writestr(it, payload)
    return out.getvalue(), {"type_fixed": fixed, "stale_overrides": stale}


def main() -> None:
    if len(sys.argv) < 2:
        print("用法：python3 repair_docx_media_types.py a.docx b.docx ...")
        sys.exit(1)
    for path in sys.argv[1:]:
        data = open(path, "rb").read()
        new, stats = repair(data)
        if stats["type_fixed"] or stats["stale_overrides"]:
            open(path, "wb").write(new)
            print(
                f"{path}: 修正媒体类型 {stats['type_fixed']} 条，"
                f"删除悬空 Override {stats['stale_overrides']} 条"
            )
        else:
            print(f"{path}: 无需修复")


if __name__ == "__main__":
    main()
