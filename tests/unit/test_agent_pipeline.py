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


def test_progress_stage_maps_real_signals():
    assert agent_pipeline._progress_stage(0, 0, 0, 0, "")[0] == "analyzing"
    assert agent_pipeline._progress_stage(1, 0, 0, 0, "")[0] == "waiting_user"
    assert agent_pipeline._progress_stage(0, 0, 1, 0, "正在撰写技术方案")[0] == "writing"
    assert agent_pipeline._progress_stage(0, 0, 1, 0, "正在执行内置评审与评分细则核对")[0] == "reviewing"
    assert agent_pipeline._progress_stage(0, 0, 1, 1, "打包完成")[0] == "packaging"
