"""成文工具链 MCP 工具注册（新方案）：主会话自主成文的机制工具。

这些工具是"机制"：切片=底稿条目区间字节级复制、填空/追加=直接干净写入正文
（无修订、无批注，输出即终稿）、校验=模板原文+替换链复算保真、封存/打包=产物落库。
写什么、按什么顺序、何时封存打包，全部由主会话决定（skill：bidvolt-agent-pipeline 的成文阶段）。
"""
import httpx

from bidvolt_mcp.tools import BIDVOLT_API_BASE, _get, _headers


def _post(path: str, body: dict) -> dict:
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=120) as client:
        resp = client.post(path, json=body, headers=_headers())
        resp.raise_for_status()
        return resp.json()


def _resolve_template_draft(args: dict) -> dict:
    return _get(f"/api/v1/projects/{args['project_id']}/assembly/drafts")


def _get_template_outline(args: dict) -> dict:
    return _get(f"/api/v1/projects/{args['project_id']}/assembly/outline")


def _slice_template_item(args: dict) -> dict:
    return _post(
        f"/api/v1/projects/{args['project_id']}/assembly/slices",
        {"file_id": args["file_id"], "req_id": args["req_id"]},
    )


def _fill_template_slice(args: dict) -> dict:
    return _post(
        f"/api/v1/projects/{args['project_id']}/assembly/slices/{args['slice_id']}/fill",
        {"fields": args.get("fields"), "fills": args.get("fills"),
         "table_fills": args.get("table_fills")},
    )


def _append_template_slice(args: dict) -> dict:
    return _post(
        f"/api/v1/projects/{args['project_id']}/assembly/slices/{args['slice_id']}/append",
        {"nodes": args.get("nodes"), "comment": args.get("comment"), "heading": args.get("heading")},
    )


def _verify_template_slice(args: dict) -> dict:
    return _get(f"/api/v1/projects/{args['project_id']}/assembly/slices/{args['slice_id']}/verify")


def _seal_template_item(args: dict) -> dict:
    return _post(
        f"/api/v1/projects/{args['project_id']}/assembly/slices/{args['slice_id']}/seal",
        {"dir": args.get("dir", "商务文件"), "filename": args.get("filename", "")},
    )


def _build_quote_xlsx(args: dict) -> dict:
    return _post(
        f"/api/v1/projects/{args['project_id']}/assembly/xlsx",
        {"sheets": args.get("sheets")},
    )


def _package_response_zip(args: dict) -> dict:
    return _post(
        f"/api/v1/projects/{args['project_id']}/assembly/package",
        {"artifact_ids": args.get("artifact_ids"), "draft_file_id": args.get("draft_file_id")},
    )


def _ask_customer(args: dict) -> dict:
    return _post(
        f"/api/v1/projects/{args['project_id']}/agent-run/{args.get('task_id', 0)}/asks",
        {"kind": "question", "items": args.get("questions")},
    )


def _report_customer_actions(args: dict) -> dict:
    return _post(
        f"/api/v1/projects/{args['project_id']}/agent-run/{args.get('task_id', 0)}/asks",
        {"kind": "action", "items": args.get("actions")},
    )


def _download_project_material(args: dict) -> dict:
    """把项目材料的原件复制到服务器本地路径（Hermes 工作区），供直接编辑。

    Hermes 拿模板底稿原件（resolve_template_draft 返回的 file_id 对应的采购文件/模板 docx），
    复制到工作区后用 python-docx 直接编辑，再经 upload_deliverable_file 整文件上传。"""
    file_id = args["file_id"]
    save_path = str(args.get("save_path") or "")
    if not save_path.startswith(("/data/hermes/", "/tmp/")):
        raise ValueError("save_path 仅允许 /data/hermes/ 或 /tmp/ 下的路径")
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=300, follow_redirects=True) as client:
        resp = client.get(f"/api/v1/files/{file_id}/download", headers=_headers())
        resp.raise_for_status()
        data = resp.content
    with open(save_path, "wb") as f:
        f.write(data)
    return {"saved_to": save_path, "bytes": len(data)}


def _upload_deliverable_file(args: dict) -> dict:
    """整文件交付：读取本地写好的完整交付文件并上传封存。

    Hermes 用 python-docx/openpyxl（可含 matplotlib/mermaid 生成的图片）直接写完整文件，
    再经本工具上传——服务端不介入内容生成。local_path 须为服务器本地绝对路径
    （/data/hermes/... 或 /tmp/...，与 Hermes 工作区同机）。"""
    local_path = str(args.get("local_path") or "")
    name = str(args.get("name") or "")
    if not local_path.startswith(("/data/hermes/", "/tmp/")):
        raise ValueError("local_path 仅允许 /data/hermes/ 或 /tmp/ 下的文件")
    try:
        with open(local_path, "rb") as f:
            content = f.read()
    except OSError as exc:
        raise ValueError(f"无法读取 local_path：{exc}") from exc
    if not name:
        name = local_path.rsplit("/", 1)[-1]
    with httpx.Client(base_url=BIDVOLT_API_BASE, timeout=300) as client:
        resp = client.post(
            f"/api/v1/projects/{args['project_id']}/assembly/upload-file",
            data={"name": name},
            files={"file": (name.rsplit("/", 1)[-1], content, "application/octet-stream")},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


def _list_agent_artifacts(args: dict) -> dict:
    return _get(f"/api/v1/projects/{args['project_id']}/assembly/artifacts")


def _inspect_agent_artifact(args: dict) -> dict:
    return _get(f"/api/v1/projects/{args['project_id']}/assembly/artifacts/{args['artifact_id']}/inspect")


ASSEMBLY_TOOL_DEFS = [
    {
        "name": "resolve_template_draft",
        "description": (
            "成文工具：列出候选底稿 docx（按是否含《响应文件格式》章分级排序）与推荐 file_id。"
            "主会话据此选择底稿（slice_template_item 的 file_id 参数）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "handler": _resolve_template_draft,
    },
    {
        "name": "get_template_outline",
        "description": (
            "成文工具：返回《响应文件格式》模板清单（doc_template），按 价格/商务/技术 分组，"
            "每项带 req_id（作为 slice_template_item 的 item_ref）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "handler": _get_template_outline,
    },
    {
        "name": "slice_template_item",
        "description": (
            "成文工具（机制）：把底稿中该模板条目区间原文整段复制为独立文档切片（保留格式），"
            "返回 slice_id。未定位到时按解析清单内容重建并在返回 warn 中如实标注。"
            "同一切片后续 fill/append/verify/seal。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "file_id": {"type": "integer", "description": "底稿 docx 的 file_id（见 resolve_template_draft）"},
                "req_id": {"type": "integer", "description": "模板条目 req_id（见 get_template_outline）"},
            },
            "required": ["project_id", "file_id", "req_id"],
            "additionalProperties": False,
        },
        "handler": _slice_template_item,
    },
    {
        "name": "fill_template_slice",
        "description": (
            "成文工具（机制）：对切片填空——直接干净写入正文（无修订、无批注）。先按 fields（buyer/project_name/supplier/tender_no）"
            "走标准填空规则（无资料的空位原位标注【待补充：标签】——这只是「还没填完」的信号，须随后清零：填实或写「无/不适用」），"
            "再按 fills=[{find,value,comment}] 定向替换。"
            "机制边界（必读）：values 只命中「标签：空位/下划线」形态（标签+冒号紧跟空位）；"
            "无标签下划线（如「特授权____」「____（盖章）」）values 打不到——回执 remaining_blanks 里"
            "label 为空的 underscore/裸【待补充】项就是没打到的，逐个用 fills 定向替换成实值或「无/不适用」，不留待补标签。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "slice_id": {"type": "string"},
                "fields": {
                    "type": "object",
                    "description": "buyer/project_name/supplier/tender_no 为四个便捷键；"
                                    "values={模板词: {value, source}} 是通用词表填值——模板里出现什么词就填什么"
                                    "（占位符如（采购人）、标签如电话/单位地址/分标名称、任意自定义词都可以），"
                                    "值从哪取由主会话决定：企业资料库 search_assets / 采购文件 / 网络搜索 / 推断均可，"
                                    "source 仅作内部记录（不产 Word 批注，出处写文末「引用来源清单」）；label_values={词:值} 为不带来源的简写",
                },
                "fills": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "find": {"type": "string", "description": "模板原文片段"},
                            "value": {"type": "string"},
                            "comment": {"type": "string", "description": "内部记录：来源与依据（不产 Word 批注）"},
                        },
                        "required": ["find", "value"],
                    },
                },
                "table_fills": {
                    "type": "array",
                    "description": "按坐标填单元格：slice 回执 tables 清册给出每张表的表头与逐行现状（行号+各列内容），"
                                    "填哪行哪列由主会话决定（通常从第一个空数据行起连续填，表头行为 0）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "table": {"type": "integer", "description": "切片内表格序号（0 起，见 tables 清册）"},
                            "row": {"type": "integer", "description": "行号（0 起，表头为 0）"},
                            "col": {"type": "integer", "description": "列号（0 起）"},
                            "value": {"type": "string"},
                            "comment": {"type": "string", "description": "内部记录：来源与依据（不产 Word 批注）"},
                        },
                        "required": ["table", "row", "col", "value"],
                    },
                },
            },
            "required": ["project_id", "slice_id"],
            "additionalProperties": False,
        },
        "handler": _fill_template_slice,
    },
    {
        "name": "append_template_slice",
        "description": (
            "成文工具（机制）：把撰写内容直接追加为切片正文（无修订、无批注）。"
            "nodes 支持 {type:heading|paragraph|table,text/rows} 或裸字符串；"
            "heading 为总标题，按投标文体自定（方案类条目正文追加用），不传用中性默认「响应内容」。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "slice_id": {"type": "string"},
                "nodes": {"type": "array"},
                "comment": {"type": "string"},
                "heading": {"type": "string", "description": "追加节标题（投标文体，如该条目章节名）"},
            },
            "required": ["project_id", "slice_id"],
            "additionalProperties": False,
        },
        "handler": _append_template_slice,
    },
    {
        "name": "verify_template_slice",
        "description": (
            "成文工具（机制）：切片忠实性校验（干净成文口径）——模板段落按「原文+替换链」复算保真"
            "（未被直接改写/删除）+ 切片模板原文逐字⊂底稿，"
            "返回 {ok, issues, original_chars, inserted_chars, deleted_chars}。"
            "不通过时主会话应带报告回修（重新 fill/append 或重新 slice）后再 seal。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "slice_id": {"type": "string"},
            },
            "required": ["project_id", "slice_id"],
            "additionalProperties": False,
        },
        "handler": _verify_template_slice,
    },
    {
        "name": "seal_template_item",
        "description": (
            "成文工具（机制）：把切片生成为条目 docx 并落产物库（先 verify 通过再 seal），"
            "返回 artifact_id（package_response_zip 用）。切片封存后即失效。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "slice_id": {"type": "string"},
                "dir": {"type": "string", "description": "价格文件/商务文件/技术文件"},
                "filename": {"type": "string", "description": "包内文件名（如（一）响应函及报价汇总表.docx）"},
            },
            "required": ["project_id", "slice_id"],
            "additionalProperties": False,
        },
        "handler": _seal_template_item,
    },
    {
        "name": "build_quote_xlsx",
        "description": (
            "成文工具（机制）：按主会话给出的 sheets 生成报价单 xlsx 并落产物库，"
            "返回 artifact_id。rows 必须是【数组的数组】（每行是一个字符串/数字数组）。"
            "报价金额由主会话测算填写（参考公开中标价/市场行情搜索+成本测算，总价≤最高限价；"
            "依据随项目而定，随件附在备注/说明格或「报价测算说明」页）。"
            "合计机制：表中有合计行时，其合价单元格写字面量 "
            "=SUM（不要手写坐标）——服务端自动替换为该列上方全部数值的求和公式；"
            "该列上方还无数值时置空，不编造合计。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "sheets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "rows": {
                                "type": "array",
                                "items": {
                                    "type": "array",
                                    "items": {"type": ["string", "number", "null"]},
                                },
                            },
                        },
                        "required": ["name", "rows"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["project_id", "sheets"],
            "additionalProperties": False,
        },
        "handler": _build_quote_xlsx,
    },
    {
        "name": "list_agent_artifacts",
        "description": (
            "成文工具（产物自检）：列出本任务已封存的全部产物（条目 docx/报价单 xlsx/响应包 zip）"
            "的 artifact_id/kind/name/大小。验收子 agent 用它核对「导出产物」是否齐全。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "handler": _list_agent_artifacts,
    },
    {
        "name": "inspect_agent_artifact",
        "description": (
            "成文工具（产物自检）：预览单个封存产物的内容——docx 返回文本头尾/字数/【待补充】计数/修订残留计数（应为 0）；"
            "xlsx 返回各表行列数与前几行；zip 返回文件清单。验收子 agent 用它核对导出产物与成果模型是否一致"
            "（例如报价单 xlsx 是否有完整表格而不是单格占位）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "artifact_id": {"type": "integer"},
            },
            "required": ["project_id", "artifact_id"],
            "additionalProperties": False,
        },
        "handler": _inspect_agent_artifact,
    },
    {
        "name": "download_project_material",
        "description": (
            "成文工具（整文件交付通道）：把项目材料的原件复制到服务器本地路径（/data/hermes/... 或 /tmp/...），"
            "供 Hermes 用 python-docx/openpyxl 直接编辑。典型用法：resolve_template_draft 得到模板底稿 file_id"
            "→ 本工具复制原件 → 本地编辑（填空/补全/插图）→ upload_deliverable_file 整文件封存。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "integer"},
                "save_path": {"type": "string"},
            },
            "required": ["file_id", "save_path"],
            "additionalProperties": False,
        },
        "handler": _download_project_material,
    },
    {
        "name": "upload_deliverable_file",
        "description": (
            "成文工具（整文件交付通道）：把 Hermes 本地写好的完整交付文件（docx/xlsx/pdf）上传封存。"
            "推荐做法：用 python-docx/openpyxl 直接生成完整文档（可用 matplotlib/mermaid 生成架构图、"
            "数据流图、进度甘特图并插入 docx），写好后经本工具上传；与切片填空路径并列可选，"
            "服务端不介入内容生成。local_path 为服务器本地绝对路径（/data/hermes/... 或 /tmp/...）；"
            "name 为包内路径名（如 技术文件/（二）专项响应文件.docx）。上传后与切片产物同样参与打包与审计。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "local_path": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["project_id", "local_path", "name"],
            "additionalProperties": False,
        },
        "handler": _upload_deliverable_file,
    },
    {
        "name": "package_response_zip",
        "description": (
            "成文工具（机制）：把已封存产物（item_docx/xlsx 的 artifact_ids）打包为响应文件包 zip，"
            "自动附 会话记录/主会话记录.md 与 manifest.json，返回 zip 的 artifact_id"
            "（客户下载端点 /response-package 将直接取它）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "artifact_ids": {"type": "array", "items": {"type": "integer"}},
                "draft_file_id": {"type": "integer"},
            },
            "required": ["project_id"],
            "additionalProperties": False,
        },
        "handler": _package_response_zip,
    },
    {
        "name": "ask_customer",
        "description": (
            "客户交互工具：向客户批量提问（question）。调用前必须先完成三查"
            "（①企业资料库 search_assets+vision 读图取证 ②采购文件/技术规范书原件 ③公开搜索+按采购文件口径合理推断），"
            "三查都拿不到、且属「只有客户本人能提供/只能客户实体动作」的信息（如银行账户、真实签署人身份、证照原件、公章）才允许问。"
            "禁止问：资料库里有的、公开可查的、可推断的、可写「无/不适用」的、不影响直接投标的。"
            "一次批量问完所有问题（不一条一条问）。调用后不干等：工具立即返回，"
            "客户在页面回答后回答会自动回到本会话；继续推进不依赖答案的工作。"
            "每问一条都要写清 need（为什么需要）与 checked（已自查说明），问得傻会被自己暴露。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "task_id": {"type": "integer", "description": "任务 id（缺省按授权上下文解析）"},
                "questions": {
                    "type": "array",
                    "description": "批量问题（一次列完）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "q": {"type": "string", "description": "问题一句话"},
                            "need": {"type": "string", "description": "为什么需要这个信息（不答会卡住什么）"},
                            "checked": {"type": "string", "description": "已自查说明（查了哪些来源、为什么拿不到）"},
                        },
                        "required": ["q"],
                    },
                },
            },
            "required": ["project_id", "questions"],
            "additionalProperties": False,
        },
        "handler": _ask_customer,
    },
    {
        "name": "report_customer_actions",
        "description": (
            "客户交互工具：上报「提交前客户动作清单」（action）——只能客户实体完成的事"
            "（如盖章/签字/提交证照原件），服务端记录并在页面呈现给客户，客户照着做。"
            "每行一条、写明动作+位置（如「盖章：授权委托书第X页加盖公章」）。"
            "没有客户动作就不用调用。收尾前必须把仍缺的实体动作用本工具上报。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "task_id": {"type": "integer", "description": "任务 id（缺省按授权上下文解析）"},
                "actions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "客户动作清单（每行一条，写明动作+位置）",
                },
            },
            "required": ["project_id", "actions"],
            "additionalProperties": False,
        },
        "handler": _report_customer_actions,
    },
]


def register_assembly_tools() -> None:
    from bidvolt_mcp import tools as t

    for d in ASSEMBLY_TOOL_DEFS:
        if d["name"] not in t._HANDLERS:
            t.TOOL_DEFS.append(d)
            t._HANDLERS[d["name"]] = d["handler"]
