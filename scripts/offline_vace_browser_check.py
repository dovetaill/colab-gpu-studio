"""Local-only browser check for VACE draft, upload, metadata and segment planning."""

import os
import subprocess
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("OFFLINE_BROWSER_BASE", "http://127.0.0.1:18788")
assert BASE.startswith("http://127.0.0.1:"), "local-only browser origin required"

with tempfile.TemporaryDirectory(prefix="vace-browser-") as folder:
    fixture = Path(folder) / "fixture.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=16",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "16", "-c:v", "libx264",
        "-threads", "1", "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", str(fixture),
    ], check=True, timeout=60)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("OFFLINE_CHROMIUM", "/usr/bin/chromium"),
            headless=True, args=["--no-sandbox"],
        )
        context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True)
        external, errors, submissions = [], [], []

        def route(request):
            if request.request.url.startswith((BASE + "/", "data:", "blob:")):
                request.continue_()
            else:
                external.append(request.request.url)
                request.abort()

        context.route("**/*", route)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: submissions.append(request.url) if request.method == "POST" and
                request.url.endswith(("/api/gpu/jobs", "/api/gpu/batches")) else None)
        page.goto(BASE)
        page.locator("input[type=password]").fill("mock-local-only")
        page.get_by_role("button", name="进入工作台").click()
        page.get_by_role("button", name="视频重绘 / V2V").click()
        page.locator('input[type="file"][accept="video/*"]').first.set_input_files(str(fixture))
        page.locator("#gpu-video_asset_id option").filter(has_text="fixture.mp4").wait_for(state="attached")
        page.locator("#gpu-video_asset_id").select_option(label="fixture.mp4")
        page.get_by_text("160×120", exact=False).first.wait_for()
        assert page.get_by_role("button", name="立即生成").is_disabled()  # draft cannot consume CU
        page.get_by_label("长视频分段").check()
        page.get_by_role("button", name="计算分段").click()
        page.get_by_text("共 4 段", exact=False).wait_for()
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.reload()
        page.get_by_role("button", name="视频重绘 / V2V").click()
        page.locator("#gpu-video_asset_id option").filter(has_text="fixture.mp4").wait_for(state="attached")
        assert not errors and not external and not submissions, {"page_errors": errors, "external_requests": external}
        browser.close()
print("VACE browser check passed: draft, upload, metadata and four-segment plan; 0 CU")
