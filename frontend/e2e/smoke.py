"""用 headless Chromium 真实走一遍前端。

覆盖：API Key 准入 → 建 Agent（window.prompt）→ 建会话 → 流式发消息 →
四个视图 → 窄屏与手机宽度布局，并保存截图。
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
API_KEY = "oak_e2e_admin_key_0001"


def launch(p):
    """优先用系统已安装的浏览器，避免下载 Chromium。"""
    for channel in ("msedge", "chrome"):
        try:
            return p.chromium.launch(channel=channel)
        except Exception as exc:  # noqa: BLE001 - 逐个降级
            print(f"[warn] 无法使用 {channel}: {exc}")
    print("[warn] 回退到 Playwright 自带 Chromium")
    return p.chromium.launch()


def main() -> int:
    shots = Path(sys.argv[1])
    shots.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = launch(p)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(BASE, wait_until="networkidle")

        # ---- 1. 准入界面 ----
        assert page.locator(".key-input").count() == 1, "应要求输入 API Key"
        page.screenshot(path=str(shots / "00-gate.png"))
        page.fill(".key-input", API_KEY)
        page.click("button:has-text('进入')")
        page.wait_for_selector(".sidebar", timeout=10000)
        print("[ok] API Key 准入")

        # ---- 2. 新建 Agent（走 window.prompt）----
        page.once("dialog", lambda dialog: dialog.accept("demo-agent"))
        page.click(".label button")
        page.wait_for_selector(".agent", timeout=10000)
        print("[ok] 新建 Agent:", page.inner_text(".agent strong"))

        # ---- 3. 新建会话 ----
        page.click("button:has-text('新建会话')")
        page.wait_for_selector(".chat-head", timeout=10000)
        print("[ok] 新建会话:", page.inner_text(".chat-head b"))

        # ---- 4. 流式发消息 ----
        page.fill(".composer textarea", "你好，做个自我介绍")
        page.press(".composer textarea", "Enter")

        # 等到流结束：发送按钮回到「发送 ↑」且已有助手消息
        page.wait_for_function(
            """() => {
                const button = document.querySelector('.composer button.primary');
                const answer = document.querySelector('.message.assistant p');
                return button && button.textContent.includes('发送')
                    && answer && answer.textContent.length > 5;
            }""",
            timeout=20000,
        )
        answer = page.inner_text(".message.assistant p")
        print("[ok] 流式回答:", answer[:28].replace("\n", " "))
        page.screenshot(path=str(shots / "01-chat.png"))

        # ---- 5. 四个视图 ----
        for label, name in [("工具", "02-tools"), ("运行", "03-runs"), ("组织", "04-org")]:
            page.click(f".nav button:has-text('{label}')")
            page.wait_for_timeout(800)
            page.screenshot(path=str(shots / f"{name}.png"))
            print(f"[ok] 视图 {label}: 标题 =", page.inner_text("header h1"))

        # ---- 6. 窄屏 / 手机宽度 ----
        page.click(".nav button:has-text('对话')")
        page.set_viewport_size({"width": 900, "height": 820})
        page.wait_for_timeout(500)
        page.screenshot(path=str(shots / "05-narrow-900.png"))
        sidebar = page.locator(".sidebar").bounding_box()
        print("[ok] 900px 下侧栏宽度:", round(sidebar["width"]))
        assert sidebar["width"] > 800, "窄屏时侧栏应变成整宽顶栏"

        page.set_viewport_size({"width": 420, "height": 820})
        page.wait_for_timeout(500)
        page.screenshot(path=str(shots / "06-mobile-420.png"))
        sidebar = page.locator(".sidebar").bounding_box()
        print("[ok] 420px 下侧栏宽度:", round(sidebar["width"]))
        assert sidebar["width"] > 380, "手机宽度下侧栏应仍为整宽"

        # 页面不应出现横向滚动（表格改用自身滚动）
        overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        print("[ok] 横向溢出像素:", overflow)
        assert overflow <= 1, "手机宽度下不应出现整页横向滚动"

        # ---- 7. 错误提示 ----
        page.set_viewport_size({"width": 1280, "height": 900})
        page.click(".nav button:has-text('工具')")
        page.wait_for_timeout(400)
        # 故意用一个非法 schema 触发后端 422
        page.fill(".stack input >> nth=0", "bad-tool")
        page.fill(".stack textarea", "{not json}")
        page.click("button:has-text('注册工具')")
        page.wait_for_selector(".error", timeout=5000)
        print("[ok] 错误提示:", page.inner_text(".error span")[:40])
        page.screenshot(path=str(shots / "07-error.png"))

        browser.close()

    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
