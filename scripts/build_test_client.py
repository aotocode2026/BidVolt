#!/usr/bin/env python3
"""打包 BidVolt 测试客户端（Issue #5）：静态站点 zip，含版本说明与启动指引。

用法：python scripts/build_test_client.py [版本号]
产物：dist/bidvolt-test-client-<版本>-static.zip
内容：app/static（index.html / app.js / style.css）+ 使用说明（README-TEST-CLIENT.txt）
说明：客户端为纯静态页（无构建步骤），任意静态服务器即可分发；支持多环境配置与连接测试。
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "app" / "static"
DIST = REPO / "dist"

USAGE = """BidVolt 测试客户端使用说明
=========================

1. 分发：把解压后的目录放到任意静态 Web 服务器（如 nginx、python -m http.server 8080）
   或直接由后端 /demo/ 提供（同源）。
2. 打开首页 →「设置/连接」页：
   - 保存环境：填写环境名与后端地址（如 http://127.0.0.1:8123 或 https://<域名>），留空表示同源；
   - 点击「测试连接」验证 healthz 与 OpenAPI 是否可达；
   - 支持保存多个环境并随时切换（保存在浏览器 localStorage，不携带任何真实账号/密钥）。
3. 其余页签（认证/项目/资料/成果/任务评标/报价/导出/搜索对话）均真实调用后端 API，
   覆盖全业务流程，可用于部署验收冒烟。
4. 安全：本客户端默认不带任何真实地址、账号、密码、Token 或 API Key；
   后端需开放 CORS（CORS_ORIGINS 配置）或使用同源反代。
"""


def main() -> None:
    version = sys.argv[1] if len(sys.argv) > 1 else None
    if not version:
        main_py = (REPO / "app" / "main.py").read_text(encoding="utf-8")
        m = re.search(r'version="([^"]+)"', main_py)
        version = m.group(1) if m else "0.0.0"
    DIST.mkdir(exist_ok=True)
    out = DIST / f"bidvolt-test-client-{version}-static.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(STATIC.iterdir()):
            if path.is_file():
                zf.write(path, arcname=f"bidvolt-test-client-{version}/{path.name}")
        zf.writestr(f"bidvolt-test-client-{version}/README-TEST-CLIENT.txt", USAGE)
    print(f"written {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
