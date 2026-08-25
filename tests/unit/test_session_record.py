"""会话记录精简版：过滤噪音块（进度条/中断提示/动画/banner），保留全部内容块。"""

from app.services.agent_pipeline import condense_session_markdown

MD = """# Hermes 主会话 · 全程记录

## [1] 服务 → 主会话 · 02:06:32

```text
请执行投标流程。
```

## [2] 主会话输出 · 02:06:33

```text
╭─ ⚕ Hermes ───╮
```

## [3] 主会话输出 · 02:06:34

```text
⚕ deepseek-v4-pro │ 0/1M │ [░░░] 0% │ 3s │ ⚠ YOLO
```

## [4] 主会话输出 · 02:06:35

```text
(°ロ°) brainstorming...
```

## [5] 主会话输出 · 02:06:36

```text
┊ ⚡ preparing mcp__bidvolt__search_assets…
```

## [6] 主会话输出 · 02:06:37

```text
企业资料 1 条：asset 7
```

## [7] 主会话输出 · 02:06:38

```text
⚡ mcp__bidvolt__search_assets  (  0.1s)
```

## [8] 主会话输出 · 02:06:39

```text
⚕ deepseek-v4-pro │ 1K/1M │ 0% │ 3s │ ⚠ YOLO
❯ msg=interrupt
```

## [9] 主会话输出 · 02:06:40

```text
todo 计划已列：A-G
```
"""


def test_condense_drops_noise_keeps_content():
    out = condense_session_markdown(MD)
    # 内容块保留
    assert "请执行投标流程。" in out
    assert "企业资料 1 条" in out
    assert "todo 计划已列" in out
    # 工具横幅不算噪音
    assert "preparing mcp__bidvolt__search_assets" in out
    assert "⚡ mcp__bidvolt__search_assets" in out
    # 纯噪音块被过滤
    assert "brainstorming" not in out
    assert "YOLO" not in out
    assert "msg=interrupt" not in out
    assert "╭─ ⚕ Hermes" not in out


def test_condense_mixed_block_kept_whole():
    """块内只要有一条内容行，整块保留（绝不丢正文）。"""
    md = MD.replace(
        "```text\n(°ロ°) brainstorming...\n```",
        "```text\n(°ロ°) brainstorming...\n继续写正文\n```",
    )
    out = condense_session_markdown(md)
    assert "继续写正文" in out
    assert "brainstorming" in out  # 混合块整体保留
