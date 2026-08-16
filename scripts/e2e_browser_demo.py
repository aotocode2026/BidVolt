"""浏览器 E2E：headless Chromium 模拟人操作 demo 前端，真实调用后端 API。

用法：
    # 本地（前置：起 API+worker，见 README 第 10 节）
    .venv/bin/python scripts/e2e_browser_demo.py
    # 线上服务器（真实 PG/LLM/搜索，任务轮询走 REST，最长 task-timeout 秒）
    .venv/bin/python scripts/e2e_browser_demo.py --base http://47.100.182.3:28123 --tag prod

说明：
- 任务类操作（解析/生成/校核）在页面上由 demo 前端 pollTask 轮询 30s；服务器真实 LLM
  可能更慢，本脚本改用 REST API 直接轮询任务终态（status 3/6），与页面行为互补。
- 会真实写数据（注册测试企业/项目/成果），测试账号带时间戳便于识别。
产出：output/playwright/<tag>-*.png 截图 + 控制台 PASS/FAIL 汇总（退出码 0/1）。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8123"
OUT = Path(__file__).resolve().parent.parent / "output" / "playwright"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name} {('(' + note + ')') if note else ''}", flush=True)


def wait_log(page, text: str, timeout_ms: int = 60000) -> bool:
    """等待页面底部 #log 出现指定文本。"""
    try:
        page.wait_for_function(
            "t => document.getElementById('log').innerText.includes(t)",
            arg=text,
            timeout=timeout_ms,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def shot(page, tag: str, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / f"{tag}-{name}.png"), full_page=True)


def tab(page, label: str) -> None:
    page.click(f"#tabs button:has-text('{label}')")
    page.wait_for_timeout(300)


def get_token(page) -> str:
    return page.evaluate("window.getBidvoltToken ? window.getBidvoltToken() : ''") or ""


def latest_submitted_task_id(page) -> int | None:
    """从页面日志取最近一次“任务 N 已提交”的任务号。"""
    text = page.evaluate("document.getElementById('log').innerText")
    ids = [int(m) for m in re.findall(r"任务 (\d+) 已提交", text)]
    return max(ids) if ids else None


def poll_task_api(base: str, token: str, task_id: int, timeout_s: int) -> dict:
    """REST 轮询任务终态：status 3=DONE, 6=FAILED_TERMINAL。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = httpx.get(
                f"{base}/api/v1/tasks/{task_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("status") in (3, 6):
                    return data
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    return {"status": -1, "note": f"轮询超时（{timeout_s}s）"}


def submit_and_wait(page, base: str, button_text: str, step_name: str, timeout_s: int = 240) -> bool:
    """点击任务按钮 → REST 轮询到终态；返回是否 DONE。"""
    page.click(f"button:has-text('{button_text}')")
    page.wait_for_timeout(800)
    task_id = latest_submitted_task_id(page)
    if task_id is None:
        record(step_name, False, "页面未出现任务提交日志")
        return False
    final = poll_task_api(base, get_token(page), task_id, timeout_s)
    if final.get("status") == 3:
        result = final.get("result") or {}
        note = str(result)[:90]
        record(step_name, True, f"task#{task_id} DONE {note}")
        return True
    record(step_name, False, f"task#{task_id} status={final.get('status')} {final.get('note', final.get('error', ''))}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--tag", default="e2e")
    parser.add_argument("--task-timeout", type=int, default=240)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    tag = args.tag
    demo = f"{base}/demo/"

    # 等 API 就绪
    for _ in range(30):
        try:
            if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    else:
        print("API 未就绪")
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(demo)
        page.wait_for_load_state("networkidle")
        shot(page, tag, "00-初始")

        # ---------- 1. 注册 ----------
        email = f"e2e-browser-{int(time.time())}@test.com"
        try:
            page.fill("#a-email", email)
            page.fill("#a-pwd", "Abc12345")
            page.fill("#a-name", f"浏览器E2E-{int(time.time()) % 100000}")
            page.click("button:has-text('注册')")
            record("注册", wait_log(page, "register 成功"), email)
        except Exception as e:  # noqa: BLE001
            record("注册", False, str(e)[:80])
        shot(page, tag, "01-注册")

        # ---------- 2. 创建项目（含空名称前端拦截回归：产品反馈） ----------
        try:
            tab(page, "项目")
            page.fill("#p-name", "")
            page.click("button:has-text('创建项目')")
            ok_block = wait_log(page, "项目名称不能为空", timeout_ms=10000)
            record("空项目名前端拦截", ok_block)
            page.fill("#p-name", "浏览器E2E项目")
            page.fill("#p-no", f"E2E-{int(time.time())}")
            page.click("button:has-text('创建项目')")
            record("创建项目", wait_log(page, "项目创建成功"))
        except Exception as e:  # noqa: BLE001
            record("创建项目", False, str(e)[:80])
        shot(page, tag, "02-项目")

        # ---------- 3. 上传材料 + 招标解析（真实 LLM 抽取） ----------
        try:
            tender = Path(__file__).resolve().parent.parent / "output" / "e2e_tender.txt"
            tender.parent.mkdir(parents=True, exist_ok=True)
            tender.write_text(
                "招标公告\n一、资质要求：投标人须具备电力工程施工总承包资质。\n"
                "二、技术要求：电缆 YJV-3x95 需符合 GB/T 12706 标准。\n"
                "三、商务要求：投标保证金 2 万元，履约保证金 5%。\n",
                encoding="utf-8",
            )
            tab(page, "资料")
            page.set_input_files("#m-file", str(tender))
            page.click("button:has-text('上传')")
            ok_up = wait_log(page, "上传 e2e_tender.txt")
            shot(page, tag, "03-上传")
            record("上传+招标解析", ok_up and submit_and_wait(page, base, "触发招标解析任务", "招标解析任务", args.task_timeout))
        except Exception as e:  # noqa: BLE001
            record("上传+招标解析", False, str(e)[:80])
        shot(page, tag, "04-解析完成")

        # ---------- 3b. Requirement 管理闭环（Issue #8/#10：确认/修正/资料匹配；解析为 0 条时手动兜底 upsert） ----------
        try:
            tab(page, "要求/匹配")
            ok_list = wait_log(page, "已加载要求", timeout_ms=30000)
            has_rows = page.eval_on_selector_all("#r-rows .r-confirm", "els => els.length > 0")
            if not has_rows:
                # LLM 抽取偶发为 0 条：手动 upsert 一条后继续闭环（同时覆盖手动新增功能点）
                page.fill("#r-content", "电力施工总承包三级资质（E2E 手动兜底）")
                page.click("button:has-text('新增要求')")
                wait_log(page, "已写入", timeout_ms=30000)
                page.wait_for_timeout(500)
            page.click("#r-rows .r-confirm")
            ok_confirm = wait_log(page, "已确认", timeout_ms=30000)
            page.click("#r-rows .r-pick")
            ok_pick = wait_log(page, "已选中要求", timeout_ms=30000)
            page.fill("#r-correct", "三级电力施工总承包资质（E2E 人工修正）")
            page.click("button:has-text('修正选中')")
            ok_correct = wait_log(page, "已修正", timeout_ms=30000)
            ok_match = submit_and_wait(page, base, "发起资料匹配", "资料匹配", args.task_timeout)
            record("Requirement 管理闭环", ok_list and ok_confirm and ok_pick and ok_correct and ok_match,
                   f"list={ok_list} confirm={ok_confirm} pick={ok_pick} correct={ok_correct} match={ok_match}")
        except Exception as e:  # noqa: BLE001
            record("Requirement 管理闭环", False, str(e)[:80])
        shot(page, tag, "04b-要求闭环")

        # ---------- 4. 成果：创建/生成/校核/在线编辑 ----------
        try:
            tab(page, "成果")
            page.click("button:has-text('创建三份成果')")
            ok_create = wait_log(page, "三份成果已创建")
            ok_gen = submit_and_wait(page, base, "生成标书(bid_generate)", "生成标书", args.task_timeout)
            ok_review = submit_and_wait(page, base, "校核(bid_review)", "校核", args.task_timeout)
            shot(page, tag, "05-成果")
            page.click("button:has-text('创建编辑会话')")
            ok_ses = wait_log(page, "编辑会话 #")
            page.click("button:has-text('保存检查点')")
            ok_ckpt = wait_log(page, "检查点已保存")
            page.click("button:has-text('完成编辑')")
            ok_done = wait_log(page, "编辑完成 →")
            record("成果/校核/在线编辑", ok_create and ok_gen and ok_review and ok_ses and ok_ckpt and ok_done)
        except Exception as e:  # noqa: BLE001
            record("成果/校核/在线编辑", False, str(e)[:80])
        shot(page, tag, "06-编辑完成")

        # ---------- 4b. 成果内容质量（Issue #7 原则：任务 done ≠ 成果可用；产品反馈回归） ----------
        try:
            quality = page.evaluate(
                """async () => {
              const base = location.origin;
              const h = { Authorization: 'Bearer ' + (window.getBidvoltToken ? window.getBidvoltToken() : '') };
              const resp = await fetch(base + '/api/v1/deliverables?project_id=' + projectId, { headers: h });
              const payload = await resp.json();
              const items = Array.isArray(payload) ? payload : (payload.items || []);
              const tech = items.find(d => d.deliverable_type === 2);
              const biz = items.find(d => d.deliverable_type === 1);
              if (!tech || !biz) return { error: '缺少技术标/商务标成果' };
              const tc = await (await fetch(base + '/api/v1/deliverables/' + tech.deliverable_id + '/content', { headers: h })).json();
              const bc = await (await fetch(base + '/api/v1/deliverables/' + biz.deliverable_id + '/content', { headers: h })).json();
              const t = tc.model.nodes.map(n => n.text || '').join('\\n');
              const b = bc.model.nodes.map(n => n.text || '').join('\\n');
              return {
                tlen: t.length,
                blen: b.length,
                techStub: t.includes('草稿由 BidVolt 确定性生成'),
                bizStub: b.includes('草稿由 BidVolt 确定性生成'),
                techHead: t.slice(0, 80),
              };
            }"""
            )
            assert quality.get("error") is None, quality
            assert quality["tlen"] >= 200, f"技术标正文过短（{quality['tlen']} 字）: {quality.get('techHead')}"
            assert not quality["techStub"], "技术标仍是确定性占位草稿（产品反馈回归未修复）"
            assert quality["blen"] >= 100, f"商务标正文过短（{quality['blen']} 字）"
            assert not quality["bizStub"], "商务标仍是确定性占位草稿"
            record("成果内容质量", True, f"技术标 {quality['tlen']} 字 / 商务标 {quality['blen']} 字，均非占位草稿")
        except Exception as e:  # noqa: BLE001
            record("成果内容质量", False, str(e)[:120])
        shot(page, tag, "06b-内容质量")

        # ---------- 5. 模拟评标（逐条 + 建议 override + 确认 + 重审） ----------
        try:
            tab(page, "任务/评标")
            page.click("button:has-text('模拟评标')")
            ok_ev = wait_log(page, "评标完成：总分")
            page.wait_for_selector("#t-items tr td", timeout=15000)  # 等待明细渲染
            item_id = page.eval_on_selector("#t-items tr td:first-child", "el => el.innerText")
            page.fill("#s-item-id", item_id.strip())
            page.fill("#s-suggestion", f"E2E-{tag} 修改后的建议文本")
            page.click("button:has-text('保存建议修改')")
            ok_sugg = wait_log(page, "建议已保存")
            shot(page, tag, "07-评标")
            page.click("button:has-text('确认全部建议')")
            ok_conf = wait_log(page, "确认结果")
            page.click("button:has-text('重审受影响项')")
            ok_re = wait_log(page, "重审完成")
            record("评标闭环", ok_ev and ok_sugg and ok_conf and ok_re, f"item={item_id.strip()}")
        except Exception as e:  # noqa: BLE001
            log_tail = page.evaluate("document.getElementById('log').innerText.split('\\n').slice(0,4).join(' | ')")
            record("评标闭环", False, f"{str(e)[:60]} LOG={log_tail[:120]}")
        shot(page, tag, "08-重审")

        # ---------- 6. 报价：测算/策略/应用/趋势 ----------
        try:
            tab(page, "报价")
            page.click("button:has-text('测算')")
            ok_calc = wait_log(page, "测算完成 calc#")
            page.click("button:has-text('中标策略')")
            ok_win = wait_log(page, "策略 win")
            page.click("button:has-text('应用到报价单')")
            ok_apply = wait_log(page, "报价已应用")
            page.click("button:has-text('样本趋势')")
            ok_trend = wait_log(page, "样本趋势")
            record("报价闭环", ok_calc and ok_win and ok_apply and ok_trend)
        except Exception as e:  # noqa: BLE001
            record("报价闭环", False, str(e)[:80])
        shot(page, tag, "09-报价")

        # ---------- 7. 会话/搜索（服务器为真实 LLM + 真实 AnySearch） ----------
        try:
            tab(page, "搜索/对话")
            page.click("button:has-text('新建会话')")
            ok_conv = wait_log(page, "已创建会话 #")
            # 等待会话下拉选中真实 ID（新建后前端会自动选中，公网延迟下必须等待就绪再发送）
            page.wait_for_function("() => /^\\d+$/.test(document.getElementById('c-sel').value)", timeout=15000)
            page.fill("#c-msg", "投标保证金一般是多少？")
            page.click("button:has-text('发送')")
            ok_reply = wait_log(page, "助手（", timeout_ms=300000)
            record("项目助手会话", ok_conv and ok_reply)
        except Exception as e:  # noqa: BLE001
            record("项目助手会话", False, str(e)[:80])
        try:
            page.fill("#s-query", "电缆 中标价")
            page.click("#panel button:has-text('搜索')")  # 精确定位面板内按钮，避免命中“搜索/对话”标签
            ok_search = wait_log(page, "搜索返回", timeout_ms=60000)
            record("搜索", ok_search, "真实 AnySearch（匿名额度/Key）")
        except Exception as e:  # noqa: BLE001
            log_tail = page.evaluate("document.getElementById('log').innerText.split('\\n').slice(0,4).join(' | ')")
            record("搜索", False, f"{str(e)[:60]} LOG={log_tail[:120]}")
        shot(page, tag, "10-会话")

        # ---------- 7b. 企业知识检索（Issue #4，来源可追溯） ----------
        try:
            page.fill("#k-query", "电缆 供货方案")
            page.click("button:has-text('知识检索')")
            ok_kn = wait_log(page, "知识检索命中", timeout_ms=30000)
            record("知识检索", ok_kn)
        except Exception as e:  # noqa: BLE001
            record("知识检索", False, str(e)[:80])

        # ---------- 7c. 招标公告 URL 导入（Issue #6：SSRF 拒绝内网，落审计） ----------
        try:
            tab(page, "资料")
            page.fill("#n-url", "http://127.0.0.1/blocked-notice.html")
            page.click("button:has-text('导入公告')")
            ok_imp = wait_log(page, "公告导入：status=3", timeout_ms=30000)
            page.click("button:has-text('公告列表')")
            ok_list = wait_log(page, "公告导入记录", timeout_ms=30000)
            record("公告导入（SSRF 防护）", ok_imp and ok_list)
        except Exception as e:  # noqa: BLE001
            record("公告导入（SSRF 防护）", False, str(e)[:80])

        # ---------- 8. 终检与导出 ----------
        try:
            tab(page, "导出")
            page.click("button:has-text('终稿检查')")
            ok_check = wait_log(page, "终检")
            page.click("button:has-text('导出 DOCX/XLSX')")
            ok_export = wait_log(page, "导出完成", timeout_ms=120000)
            record("终检与导出", ok_check and ok_export)
        except Exception as e:  # noqa: BLE001
            record("终检与导出", False, str(e)[:80])
        shot(page, tag, "11-导出")

        # ---------- 9. 快照/活动任务 ----------
        try:
            tab(page, "项目")
            page.click("button:has-text('快照列表')")
            ok_snap = page.wait_for_function(
                "document.getElementById('p-extra').innerText.includes('snapshot_id')",
                timeout=20000,
            )
            page.click("button:has-text('活动任务')")
            ok_tasks = page.wait_for_function(
                "document.getElementById('p-extra').innerText.includes('task_id')",
                timeout=20000,
            )
            record("快照/活动任务", bool(ok_snap and ok_tasks))
        except Exception as e:  # noqa: BLE001
            extra = page.evaluate("document.getElementById('p-extra').innerText")
            log_tail = page.evaluate("document.getElementById('log').innerText.split('\\n').slice(0,4).join(' | ')")
            record("快照/活动任务", False, f"{str(e)[:50]} EXTRA={extra[:60]} LOG={log_tail[:100]}")
        shot(page, tag, "12-快照")

        # ---------- 10. 刷新恢复（会话与数据） ----------
        try:
            page.reload()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            logged = page.eval_on_selector("#authbar", "el => el.innerText.includes('已登录')")
            tab(page, "项目")
            page.wait_for_timeout(800)
            restored = page.eval_on_selector(
                "#p-rows",
                "el => el.innerText.includes('浏览器E2E项目')",
            )
            record("刷新恢复", logged and restored, "localStorage token + 项目列表")
        except Exception as e:  # noqa: BLE001
            record("刷新恢复", False, str(e)[:80])
        shot(page, tag, "13-刷新恢复")

        # ---------- 10b. 测试客户端：环境切换 + 连接测试（Issue #5/#10，放最后：环境切换会退出登录） ----------
        try:
            tab(page, "设置/连接")
            page.click("button:has-text('测试连接')")
            ok_conn = wait_log(page, "连接测试", timeout_ms=30000)
            page.fill("#env-name", f"本地-{tag}")
            page.fill("#env-base", base)
            page.click("button:has-text('保存环境')")
            ok_env = wait_log(page, "已保存并切换环境", timeout_ms=30000)
            logged_out = page.eval_on_selector("#authbar", "el => el.innerText.includes('未登录')")
            record("测试客户端（连接测试/环境保存）", ok_conn and ok_env and logged_out,
                   f"conn={ok_conn} env={ok_env} logged_out={logged_out}")
        except Exception as e:  # noqa: BLE001
            record("测试客户端（连接测试/环境保存）", False, str(e)[:80])
        shot(page, tag, "13b-设置连接")

        browser.close()

    failed = [r for r in results if not r[1]]
    print(f"\n==== 汇总：{len(results) - len(failed)}/{len(results)} PASS (base={base}, tag={tag}) ====", flush=True)
    for name, ok, note in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  {note}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
