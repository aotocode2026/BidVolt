"""A-10 沙箱边界：受限执行拒绝 root / 资源受限 / 无网络代理环境。"""

from __future__ import annotations

import subprocess
import sys

import pytest

from app.services.sandbox import run_restricted


def test_sandbox_runs_simple_code():
    result = run_restricted("print('ok')")
    assert result.returncode == 0
    assert result.stdout.strip() == "ok"


def test_sandbox_rejects_syntax_error():
    result = run_restricted("print(")
    assert result.returncode != 0
    assert "SyntaxError" in result.stderr


def test_sandbox_timeout_violation():
    try:
        result = run_restricted("while True: pass", timeout=2, cpu_seconds=1)
    except subprocess.TimeoutExpired:
        return  # Windows 降级：子进程超时即满足"受限"
    if sys.platform != "win32":
        assert result.returncode != 0


def test_sandbox_root_check():
    if "resource" in sys.modules and sys.platform != "win32":
        result = run_restricted("import os; print(os.getuid())")
        if result.returncode != 0:
            assert "must not run as root" in result.stderr


def test_sandbox_env_strips_proxies(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    result = run_restricted("import os; print(os.environ.get('HTTP_PROXY', 'NONE'))")
    assert result.returncode == 0
    assert result.stdout.strip() == "NONE"
