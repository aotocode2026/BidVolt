"""hermes 消毒器反馈补丁（幂等，可重复应用；pip 升级 hermes 后重跑一次即可）。

背景：hermes 的 _repair_tool_call_arguments 在参数 JSON 损坏且无法修复时，
原实现静默返回 "{}"——工具用空参执行（terminal 空命令/写文件丢内容），
模型看到的只是"成功或莫名失败"，不知道自己参数坏了，会重复犯错。
本补丁把最后手段改成携带错误说明的失败载荷：工具必然校验失败，
错误会作为 tool result 摆到模型面前，模型可自行改写重试（配合 SKILL 分批写入守则）。

应用方式：python scripts/apply_hermes_unrepairable_feedback.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PATCH_ROOT = Path("/data/hermes/venv/lib/python3.11/site-packages")
TARGET = PATCH_ROOT / "agent" / "message_sanitization.py"

OLD = '''    # Last resort: replace with empty object so the API request doesn't
    # crash the entire session.
    logger.warning(
        "Unrepairable tool_call arguments for %s — "
        "replaced with empty object (was: %s)",
        tool_name, raw_stripped[:80],
    )
    return "{}"
'''

NEW = '''    # Last resort (BidVolt patch): carry an explanatory error payload instead
    # of a silent empty object. The tool call then fails schema validation and
    # the failure is surfaced to the model as a tool result — the model learns
    # that ITS argument JSON was malformed and can retry with a better form
    # (e.g. chunked writes instead of one giant heredoc).
    logger.warning(
        "Unrepairable tool_call arguments for %s — "
        "replaced with error payload (was: %s)",
        tool_name, raw_stripped[:80],
    )
    return json.dumps(
        {
            "__bidvolt_unrepairable_args__": (
                "本次工具调用参数 JSON 损坏且无法自动修复，已忽略原调用并注入此失败载荷。"
                "请改写后重试：超长中文文本请分批写入（单次参数 <2000 字），"
                "避免未转义换行/引号；不要用单条 heredoc 塞整份文档。"
                f"原始参数片段: {raw_stripped[:160]!r}"
            )
        },
        ensure_ascii=False,
    )
'''


def main() -> int:
    if not TARGET.exists():
        print("未找到 hermes 安装：", TARGET, file=sys.stderr)
        return 2
    text = TARGET.read_text(encoding="utf-8")
    if NEW.strip() in text:
        print("已打过补丁，跳过。")
        return 0
    if OLD.strip() not in text:
        print("锚点不匹配（hermes 版本变化？），未修改。请人工核对：", TARGET, file=sys.stderr)
        return 3
    TARGET.write_text(
        text.replace(OLD, NEW, 1), encoding="utf-8"
    )
    print("补丁已应用：", TARGET)
    return 0


if __name__ == "__main__":
    sys.exit(main())
