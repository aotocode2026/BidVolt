"""聚焦复现评标闭环：登录 prod15 账号，模拟评标→保存建议→确认→重审，转储日志。"""
import time

from playwright.sync_api import sync_playwright

BASE = "http://47.100.182.3:28123"
EMAIL = "e2e-browser-1787239674@test.com"
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
    # 选用项目（该账号的项目）
    page.wait_for_function("() => document.querySelectorAll('#p-rows .row-use').length >= 1", timeout=30000)
    page.click("#p-rows .row-use")
    time.sleep(1)
    page.click("#tabs button:has-text('评审')")
    time.sleep(3)
    print("==== 进入评审页日志 ====")
    for line in log_text(page).splitlines()[-6:]:
        print("  ", line.strip()[:150])
    page.click("button:has-text('模拟评标')")
    try:
        page.wait_for_function("t => document.getElementById('log').innerText.includes(t)", arg="评标完成", timeout=60000)
    except Exception:
        pass
    time.sleep(2)
    try:
        page.wait_for_selector("#t-items tr td", timeout=10000)
        item_id = page.eval_on_selector("#t-items tr td:first-child", "el => el.innerText")
        page.fill("#s-item-id", item_id.strip())
        page.fill("#s-suggestion", "debug-修改后的建议")
        page.click("button:has-text('保存建议修改')")
        time.sleep(1)
        page.click("button:has-text('确认全部建议')")
        time.sleep(2)
        page.click("button:has-text('重审受影响项')")
        time.sleep(3)
    except Exception as e:  # noqa: BLE001
        print("[step error]", str(e)[:120])
    print("==== 终态日志 ====")
    for line in log_text(page).splitlines()[-14:]:
        print("  ", line.strip()[:180])
    browser.close()
