"""Exercise smoke-status reconnection without creating any GPU job."""

import os

from playwright.sync_api import sync_playwright

BASE = os.environ.get("OFFLINE_BROWSER_BASE", "http://127.0.0.1:18788")
assert BASE.startswith("http://127.0.0.1:"), "local-only browser origin required"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        executable_path=os.environ.get("OFFLINE_CHROMIUM", "/usr/bin/chromium"),
        headless=True, args=["--no-sandbox"],
    )
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True)
    calls, writes, errors = [], [], []

    def route(request):
        url = request.request.url
        if not url.startswith((BASE + "/", "data:", "blob:")):
            request.abort()
            return
        if request.request.method == "POST" and not url.endswith("/api/auth/login"):
            writes.append(url)
            request.abort()
            return
        if request.request.method == "GET" and url.endswith("/activation"):
            calls.append(url)
            if len(calls) == 2:
                request.abort("failed")
                return
            response = request.fetch()
            body = response.json()
            snapshot = body["data"]
            state = "preparing" if len(calls) == 1 else "completed"
            snapshot["smoke"] = {"job_id": "offline-smoke", "state": state, "acceptance_state": "pending"}
            snapshot["smoke_job_completed"] = state == "completed"
            for step in snapshot["steps"]:
                if step["key"] == "real_smoke":
                    step["running"] = state != "completed"
                    step["done"] = state == "completed"
            request.fulfill(response=response, json=body)
            return
        request.continue_()

    context.route("**/*", route)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE)
    page.locator("input[type=password]").fill("mock-local-only")
    page.get_by_role("button", name="进入工作台").click()
    page.get_by_role("button", name="打开设置", exact=True).click()
    page.get_by_role("button", name="工作流与模型", exact=False).click()
    page.get_by_label("搜索工作流", exact=True).fill("wan21-vace-v2v")
    group = page.locator("article").filter(has_text="wan21-vace-v2v")
    group.locator("button").first.click()
    group.get_by_role("button", name="一键启用").click()
    group.get_by_text("暂时无法读取冒烟状态，正在重试", exact=False).wait_for(timeout=20000)
    group.get_by_text("我已播放冒烟输出", exact=False).wait_for(timeout=20000)
    assert len(calls) >= 3
    assert group.get_by_role("button", name="继续验收并启用").is_disabled()
    assert not writes and not errors, {"blocked_writes": len(writes), "page_errors": errors}
    browser.close()

print("Smoke status reconnected to the existing job; no GPU submission or CU usage")
