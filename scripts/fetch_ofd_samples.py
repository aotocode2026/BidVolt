"""下载真实 OFD 样本（第三方/开源，GB/T 33190）用于解析复验。

来源：
- ElysiaTools Simple OFD：https://elysiatools.com/en/samples/ofd-samples（GB/T 33190 最小样例）
- OFD.js（DLTech21/ofd.js，js 分支 public/）：真实中文 OFD（多页招标/发票/扫描版/带签章）

用法：python scripts/fetch_ofd_samples.py [--dir output/playwright/ofd_samples]
GitHub API 未认证限 60 次/小时，本脚本仅需 ~5 次调用；可设置 GH_TOKEN 提升额度。
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path

SAMPLES = {
    "simple.ofd": "https://api.elysiatools.com/public/samples/ofd/simple.ofd",
}
OFDJS_REPO = "DLTech21/ofd.js"
OFDJS_BRANCH = "js"
OFDJS_FILES = ["2.ofd", "999.ofd", "h.ofd", "n.ofd"]


def _request(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Codex/BidVolt-ofd-sample-fetcher"})
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _github_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Codex/BidVolt-ofd-sample-fetcher", "Accept": "application/json"})
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _github_raw(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Codex/BidVolt-ofd-sample-fetcher", "Accept": "application/vnd.github.raw"},
    )
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="output/playwright/ofd_samples")
    args = ap.parse_args()
    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)

    for name, url in SAMPLES.items():
        (out / name).write_bytes(_request(url))
        print(f"OK {name}")

    tree = _github_json(f"https://api.github.com/repos/{OFDJS_REPO}/git/trees/{OFDJS_BRANCH}?recursive=1")
    blobs = {b["path"]: b["sha"] for b in tree.get("tree", []) if b.get("path", "").endswith(".ofd")}
    for name in OFDJS_FILES:
        path = f"public/{name}"
        sha = blobs.get(path)
        if not sha:
            print(f"SKIP {path} (not in repo)")
            continue
        raw = _github_raw(f"https://api.github.com/repos/{OFDJS_REPO}/git/blobs/{sha}")
        (out / f"ofdjs-{name}").write_bytes(raw)
        print(f"OK ofdjs-{name} ({len(raw)} bytes)")


if __name__ == "__main__":
    main()
