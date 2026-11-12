"""Read-only browser check of the seeded hackathon story, desktop and mobile."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8765')
    args = parser.parse_args()
    address = urlparse(args.base_url)
    if address.hostname not in {'localhost', '127.0.0.1'} or address.port != 8765:
        parser.error('Use the owned isolated QA server on localhost:8765.')
    base = args.base_url.rstrip('/')
    output = Path('test-results')
    output.mkdir(exist_ok=True)
    errors, checks = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for width in (1440, 390):
            context = browser.new_context(viewport={'width': width, 'height': 1000})
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(base + '/accounts/login/')
            page.get_by_label('Имя пользователя').fill('hack_processor')
            page.get_by_label('Пароль', exact=True).fill(os.getenv('DEMO_PASSWORD', 'ModusDemo2026!'))
            page.get_by_role('button', name='Войти в пространство').click()
            page.wait_for_url(base + '/')
            if width == 390:
                page.locator('[data-menu-toggle]').click()
            page.get_by_role('link', name='Повторяющиеся проблемы', exact=True).click()
            expect(page.get_by_text('Повторов: 3', exact=True)).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / f'recurring-{width}.png'), full_page=True)
            page.locator('article a.ticket-title').click()
            expect(page.locator('#related a')).to_have_count(3)
            parent_url = page.url
            page.locator('#related a').first.click()
            expect(page.locator('#related')).to_contain_text('Основное обращение:')
            page.locator('#related a').click()
            assert page.url == parent_url
            page.screenshot(path=str(output / f'related-{width}.png'), full_page=True)
            page.goto(base + '/recurring/?mode=production')
            expect(page.get_by_text('Повторов: 3', exact=True)).to_have_count(0)
            checks.append(f'{width}px: navigation, three repeats, bidirectional links, production exclusion, no horizontal overflow')
            context.close()
        browser.close()
    assert not errors, errors
    result = {'checks': checks, 'page_errors': errors}
    (output / 'browser-recurrence.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
