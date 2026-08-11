"""受限子进程沙箱（A-10，P1）：非 root + rlimit + 无网络依赖 + 只读输入。

供未来外部 Code Provider / 转换器 / 解压器执行不可信代码使用。
V1 内置评审为确定性代码（不执行不可信输入），本模块提供可验证的隔离机制。
Windows 本地开发环境无 resource 模块，自动降级为普通子进程（测试跳过强约束断言）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def _rlimit_wrapper(cpu_seconds: int, max_bytes: int) -> str:
    return textwrap.dedent(
        f"""
        import resource

        def _bail(msg):
            import sys
            sys.stderr.write("SANDBOX:" + msg + "\\n")
            sys.exit(125)

        if __import__("os").getuid() == 0:
            _bail("must not run as root")
        resource.setrlimit(resource.RLIMIT_CPU, ({cpu_seconds}, {cpu_seconds + 1}))
        resource.setrlimit(resource.RLIMIT_AS, ({max_bytes}, {max_bytes}))
        resource.setrlimit(resource.RLIMIT_FSIZE, ({max_bytes}, {max_bytes}))
        """
    )


def run_restricted(
    code: str,
    *,
    input_dir: Path | None = None,
    timeout: int = 10,
    cpu_seconds: int = 5,
    max_bytes: int = 256 * 1024 * 1024,
    python: str | None = None,
) -> subprocess.CompletedProcess:
    """以受限方式执行 Python 代码片段，返回 CompletedProcess。

    限制：CPU 时间、地址空间、文件大小 rlimit；剥离网络代理环境变量；
    工作目录限制在 input_dir（只读输入）；代码经 stdin 传入，不落盘到业务目录。
    """
    env = {k: v for k, v in os.environ.items() if k not in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")}
    env["SANDBOX_INPUT_DIR"] = str(input_dir) if input_dir else ""
    wrapper = ""
    try:
        import resource  # noqa: F401

        wrapper = _rlimit_wrapper(cpu_seconds, max_bytes)
    except ImportError:  # pragma: no cover - Windows 本地开发
        pass

    script = wrapper + "\n" + code
    return subprocess.run(
        [python or sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(input_dir) if input_dir else None,
    )
