"""Author summary, demo navigation and inspectable result at two widths."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8765')
    base = parser.parse_args().base_url.rstrip('/')
    address = urlparse(base)
    if address.hostname not in {'localhost', '127.0.0.1'} or address.port != 8765:
        parser.error('Use the owned localhost QA server on port 8765.')
    out = Path('test-results'); out.mkdir(exist_ok=True)
    errors, checks = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for width in (1440, 390):
            context = browser.new_context(viewport={'width': width, 'height': 1000})
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(base + '/demo/')
            page.get_by_label('Имя пользователя').fill('hack_author')
            page.get_by_label('Пароль', exact=True).fill(os.getenv('DEMO_PASSWORD', 'ModusDemo2026!'))
            page.get_by_role('button', name='Войти в пространство').click()
            page.wait_for_url(base + '/demo/')
            expect(page.get_by_role('heading', name='От обращения к результату')).to_be_visible()
            page.screenshot(path=str(out/f'demo-start-{width}.png'), full_page=True)
            page.get_by_role('link', name='Синтетическое новое обращение:', exact=False).click()
            expect(page.get_by_role('region', name='Что происходит дальше')).to_contain_text('Обращение зарегистрировано')
            page.screenshot(path=str(out/f'author-next-{width}.png'), full_page=True)
            page.goto(base + '/demo/')
            page.get_by_role('link', name='Учебный пример: опубликована', exact=False).click()
            expect(page.get_by_role('region', name='Что происходит дальше')).to_contain_text('Результат рассмотрения готов')
            page.get_by_role('link', name='Прочитать результат', exact=True).click()
            expect(page.locator('#result')).to_be_in_viewport()
            page.screenshot(path=str(out/f'author-result-{width}.png'), full_page=True)
            page.get_by_role('link', name='Открыть памятку — результат демо').click()
            expect(page.get_by_role('heading', name='Как согласовать закупку')).to_be_visible()
            expect(page.get_by_text('Не является утверждённым регламентом', exact=False)).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(out/f'demo-guide-{width}.png'), full_page=True)
            page.get_by_role('link', name='К результату обращения').click()
            expect(page.locator('#result')).to_be_in_viewport()
            checks.append(f'{width}px: login return, demo start, author next step, result anchor, guide, back navigation, no overflow')
            context.close()
        browser.close()
    assert not errors, errors
    result = {'checks': checks, 'javascript_errors': errors}
    (out/'browser-presentation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
