#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""docx 媒体压缩（修正版）：图片转 JPEG q62、最长边<=1500px，并同步 ContentType。

与事故版本的差异：不依赖扩展名重命名——媒体部件名保持原样（含无扩展名 imageN.），
重编码后按实际输出格式改写 [Content_Types].xml 的 Override，保证 Word/LibreOffice 可解码。

用法：python3 compress_docx_media.py a.docx b.docx
"""

from __future__ import annotations

import io
import re
import sys
import zipfile

from PIL import Image


def compress(data: bytes, max_side: int = 1500, quality: int = 62) -> tuple[bytes, int]:
    zin = zipfile.ZipFile(io.BytesIO(data))
    infos = zin.infolist()
    contents = {it.filename: zin.read(it.filename) for it in infos}
    touched: set[str] = set()
    for name in list(contents):
        if not name.startswith("word/media/"):
            continue
        if name.lower().endswith((".emf", ".wmf")):
            continue
        try:
            im = Image.open(io.BytesIO(contents[name])).convert("RGB")
        except Exception:
            continue
        w, h = im.size
        scale = min(1.0, max_side / max(w, h))
        if scale < 1.0:
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        contents[name] = buf.getvalue()
        touched.add(name)
    cts_name = next(
        (n for n in contents if n.lower().endswith("[content_types].xml")),
        "[Content_Types].xml",
    )
    cts = contents[cts_name].decode("utf-8")

    def _repl(m: re.Match) -> str:
        part, decl = m.group(1), m.group(2)
        entry = "word" + part.split("/word", 1)[1]
        if entry in touched and decl != "image/jpeg":
            return f'<Override PartName="{part}" ContentType="image/jpeg"/>'
        return m.group(0)

    contents[cts_name] = re.sub(
        r'<Override PartName="(/word/media/[^"]+)" ContentType="(image/[^"]+)"/>',
        _repl,
        cts,
    ).encode("utf-8")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for it in infos:
            zout.writestr(it, contents[it.filename])
    return out.getvalue(), len(touched)


def main() -> None:
    if len(sys.argv) < 2:
        print("用法：python3 compress_docx_media.py a.docx b.docx")
        sys.exit(1)
    for path in sys.argv[1:]:
        data = open(path, "rb").read()
        new, n = compress(data)
        open(path, "wb").write(new)
        print(f"{path}: compressed {n} media, {len(data)} -> {len(new)} bytes")


if __name__ == "__main__":
    main()
