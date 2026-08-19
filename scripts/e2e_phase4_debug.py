"""聚焦复现：登录种子账号 → 损坏文件项目触发解析 → 转储完整页面日志。"""
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://47.100.182.3:28123"
EMAIL = "seeded-1787158311@test.com"
PWD = "Abc12345"


def log_text(page) -> str:
    return page.evaluate("document.getElementById('log').innerText")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(f"{BASE}/demo/")
    page.wait_for_load_state("networkidle")
    page.fill("#a-email", EMAIL)
    page.fill("#a-pwd", PWD)
    page.click("#a-login")
    page.wait_for_function("t => document.getElementById('log').innerText.includes(t)", arg="login 成功", timeout=30000)
    time.sleep(2)
    # 选用损坏文件项目
    page.click("button:has-text('项目')")
    page.wait_for_timeout(500)
    page.wait_for_function("() => document.querySelectorAll('#p-rows .row-use').length >= 3", timeout=30000)
    page.click("#p-rows tr:has-text('损坏文件项目') .row-use")
    page.wait_for_timeout(500)
    page.click("button:has-text('资料')")
    page.wait_for_timeout(500)
    prev = log_text(page)
    prev_ids = [int(m) for m in re.findall(r"任务 (\d+) 已提交", prev)]
    prev_max = max(prev_ids) if prev_ids else 0
    page.click("button:has-text('触发招标解析任务')")
    page.wait_for_function(
        "max => { const t = document.getElementById('log').innerText; "
        "const ids = [...t.matchAll(/任务 (\\d+) 已提交/g)].map(m => +m[1]); "
        "return ids.length && Math.max(...ids) > max; }",
        arg=prev_max,
        timeout=30000,
    )
    # 等待终态失败日志（超时也继续，转储日志）
    try:
        page.wait_for_function("t => document.getElementById('log').innerText.includes(t)", arg="失败：文件解析失败", timeout=120000)
    except Exception as e:  # noqa: BLE001
        print("[等待超时]", str(e)[:80])
    time.sleep(2)
    print("==== 页面日志全文 ====")
    for line in log_text(page).splitlines():
        print(line.strip()[:200])
    browser.close()
