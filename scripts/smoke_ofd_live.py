# -*- coding: utf-8 -*-
"""Live check: OFD upload -> parse -> doc_block retrievable."""
import argparse, io, time, zipfile
import httpx

CONTENT_TEXT = "\u62db\u6807\u6280\u672f\u8981\u6c42\uff1a\u7535\u538b\u7b49\u7ea7 10kV\uff0c\u542b OFD \u5192\u70df\u9a8c\u8bc1"

OFD_XML = '<?xml version="1.0" encoding="UTF-8"?>\n<ofd:OFD xmlns:ofd="http://www.ofdspec.org/2016">\n  <ofd:DocBody>\n    <ofd:DocInfo><ofd:DocID>12345678901234567890123456789012</ofd:DocID></ofd:DocInfo>\n    <ofd:DocRoot><ofd:BaseLoc>Doc_0/Document.xml</ofd:BaseLoc></ofd:DocRoot>\n  </ofd:DocBody>\n</ofd:OFD>\n'
DOC_XML = '<?xml version="1.0" encoding="UTF-8"?>\n<ofd:Document xmlns:ofd="http://www.ofdspec.org/2016">\n  <ofd:CommonData>\n    <ofd:PageArea><ofd:PhysicalBox>0 0 595.0 842.0</ofd:PhysicalBox></ofd:PageArea>\n    <ofd:PublicRes>Doc_0/PublicRes.xml</ofd:PublicRes>\n    <ofd:DocumentRes>Doc_0/DocumentRes.xml</ofd:DocumentRes>\n  </ofd:CommonData>\n  <ofd:Pages>\n    <ofd:Page ID="1"><ofd:BaseLoc>Doc_0/Pages/Page_1/Content.xml</ofd:BaseLoc></ofd:Page>\n  </ofd:Pages>\n</ofd:Document>\n'
CONTENT_XML_TMPL = '<?xml version="1.0" encoding="UTF-8"?>\n<ofd:Page xmlns:ofd="http://www.ofdspec.org/2016">\n  <ofd:Content>\n    <ofd:Layer ID="4" Type="Foreground">\n      <ofd:TextObject ID="5" Font="3" Size="12" Boundary="0 0 100 20">\n        <ofd:TextCode X="0" Y="12">{CONTENT_TEXT}</ofd:TextCode>\n      </ofd:TextObject>\n    </ofd:Layer>\n  </ofd:Content>\n</ofd:Page>\n'
RES_XML = '<?xml version="1.0" encoding="UTF-8"?>\n<ofd:Res xmlns:ofd="http://www.ofdspec.org/2016"><ofd:Fonts/></ofd:Res>\n'

def make_ofd() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("OFD.xml", OFD_XML)
        zf.writestr("Doc_0/Document.xml", DOC_XML)
        zf.writestr("Doc_0/Pages/Page_1/Content.xml", CONTENT_XML_TMPL.replace("{CONTENT_TEXT}", CONTENT_TEXT))
        zf.writestr("Doc_0/PublicRes.xml", RES_XML)
        zf.writestr("Doc_0/DocumentRes.xml", RES_XML)
    return buf.getvalue()

def run(base: str) -> None:
    with httpx.Client(base_url=base, timeout=60) as c:
        email = f"ofd-live-{int(time.time())}@test.com"
        reg = c.post("/api/v1/auth/register", json={"email": email, "password": "Abc12345", "enterprise_name": "OFDSmoke"})
        reg.raise_for_status()
        headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        pid = c.post("/api/v1/projects", json={"name": "OFDSmoke"}, headers=headers).json()["project_id"]
        up = c.post(
            "/api/v1/files/upload",
            data={"target": "project", "project_id": str(pid)},
            files=[("files", ("tender-requirements.ofd", make_ofd(), "application/zip"))],
            headers=headers,
        )
        up.raise_for_status()
        f = up.json()["files"][0]
        print("upload:", f["name"], "status:", f["status"])
        assert f["status"] == 3, f"OFD parse failed: {f}"
        blocks = c.get(f"/api/v1/files/{f['file_id']}/blocks", headers=headers).json()["items"]
        texts = [b.get("text", "") for b in blocks]
        print("block texts:", texts[:3])
        assert any("10kV" in t for t in texts), "block text miss"
        assert any("\u62db\u6807" in t for t in texts), "chinese text missing"
        print("OFD LIVE PASS")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://47.100.182.3:28123")
    run(p.parse_args().base)
