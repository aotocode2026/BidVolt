"""Issue #37：主会话模型凭据预检（DeepSeek）。"""

from __future__ import annotations

from app.services import agent_pipeline


def test_credential_available_from_process_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert agent_pipeline._hermes_credential_available() is True


def test_credential_available_from_hermes_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-abc\n", encoding="utf-8")
    assert agent_pipeline._hermes_credential_available() is True


def test_credential_missing_returns_false(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("MINIMAX_API_KEY=x\n", encoding="utf-8")
    assert agent_pipeline._hermes_credential_available() is False
