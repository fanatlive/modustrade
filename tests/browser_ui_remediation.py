"""Run against an isolated *_browser database; writes synthetic QA cards only."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    address = urlparse(args.base_url)
    if address.hostname not in {"localhost", "127.0.0.1"} or address.port == 8000:
        parser.error("Use an isolated localhost QA server, not the main port 8000.")
    sqlite_path = Path(os.getenv("SQLITE_PATH", ""))
    isolated_sqlite = os.getenv("DB_ENGINE") == "sqlite" and sqlite_path.is_absolute() and sqlite_path.name.endswith("_browser.sqlite3")
    if not (isolated_sqlite or (os.getenv("DB_ENGINE") == "postgres" and os.getenv("POSTGRES_DB", "").endswith("_browser"))):
        parser.error("Set POSTGRES_DB to the isolated *_browser database used by this server.")
    base = args.base_url.rstrip("/")
    checks, errors = [], []
    output = Path("test-results")
    output.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        def login(username, mobile=False):
            context = browser.new_context(viewport={"width": 390 if mobile else 1440, "height": 900}, reduced_motion="reduce")
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base+"/accounts/login/")
            page.get_by_label("Имя пользователя").fill(username)
            page.get_by_label("Пароль", exact=True).fill(os.getenv("DEMO_PASSWORD", "ModusDemo2026!"))
            page.get_by_role("button", name="Войти в пространство").click()
            page.wait_for_url(base+"/")
            return page

        author = login("author")
        author.goto(base+"/requests/new/")
        author.locator("#id_category").select_option(label="Коммуникации")
        author.locator("#id_description").fill("Проверка исправлений аудита: согласование решений и уведомления сотрудников.")
        author.get_by_role("button", name="Зарегистрировать обращение").click()
        author.wait_for_url("**/requests/*/")
        ticket_url = author.url
        processor = login("processor")
        processor.goto(ticket_url)
        stale = processor.context.new_page()
        stale.on("pageerror", lambda error: errors.append(str(error)))
        stale.goto(ticket_url)
        processor.locator("#comment-message").fill("Параллельное изменение для проверки конфликта версий")
        processor.get_by_role("button", name="Отправить", exact=True).click()
        stale.locator("#comment-message").fill("PRIVATE-AUDIT-BROWSER-409")
        stale.locator('select[name="visibility"]').select_option("internal")
        with stale.expect_response(lambda response: response.url.endswith("/command/")) as conflict:
            stale.get_by_role("button", name="Отправить", exact=True).click()
        assert conflict.value.status == 409
        expect(stale.locator('select[name="visibility"]')).to_have_value("internal")
        expect(stale.locator("#comment-message")).to_have_value("PRIVATE-AUDIT-BROWSER-409")
        expect(stale.get_by_text("Черновик внутренней заметки сохранён.", exact=False)).to_be_visible()
        stale.get_by_role("button", name="Отправить", exact=True).click()
        expect(stale.get_by_text("PRIVATE-AUDIT-BROWSER-409", exact=True)).to_be_visible()
        author.reload()
        expect(author.get_by_text("PRIVATE-AUDIT-BROWSER-409", exact=True)).to_have_count(0)
        checks.append("409 preserves internal selection and text; retry stays hidden from author")

        mobile = login("author", mobile=True)
        sidebar = mobile.locator("#sidebar")
        expect(sidebar).to_have_attribute("inert", "")
        expect(sidebar).to_have_attribute("aria-hidden", "true")
        for _ in range(12):
            mobile.keyboard.press("Tab")
            assert not mobile.evaluate("document.querySelector('#sidebar').contains(document.activeElement)")
        checks.append("closed mobile sidebar excluded from keyboard navigation")
        toggle = mobile.locator("[data-menu-toggle]")
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        assert mobile.evaluate("document.querySelector('#sidebar').contains(document.activeElement)")
        mobile.keyboard.press("Escape")
        expect(toggle).to_be_focused()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(sidebar).to_have_attribute("inert", "")
        checks.append("mobile menu focuses navigation and returns focus on Escape")
        mobile.screenshot(path=str(output/"audit-mobile-menu.png"), full_page=True)
        mobile.set_viewport_size({"width": 1440, "height": 900})
        expect(sidebar).not_to_have_attribute("inert", "")
        expect(sidebar).not_to_have_attribute("aria-hidden", "true")
        checks.append("desktop resize restores navigation access")
        assert not errors, errors
        browser.close()
    result = {"checks": checks, "page_errors": errors}
    (output/"browser-ui-remediation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
