"""End-to-end demo check. Run against an isolated, seeded local demo database."""
import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8765')
    args = parser.parse_args()
    if urlparse(args.base_url).hostname not in {'localhost', '127.0.0.1'}:
        parser.error('This check sends demo credentials only to localhost.')
    base = args.base_url.rstrip('/')
    out = Path('test-results')
    out.mkdir(exist_ok=True)
    password = os.getenv('DEMO_PASSWORD', 'ModusDemo2026!')
    checks, errors = [], []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        contexts = []

        def login(username, mobile=False):
            context = browser.new_context(viewport={'width': 390, 'height': 844} if mobile else {'width': 1440, 'height': 1000}, reduced_motion='reduce')
            contexts.append(context)
            page = context.new_page()
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            page.goto(base+'/accounts/login/')
            page.wait_for_load_state('networkidle')
            expect(page.get_by_text('Демонстрационный стенд', exact=True)).to_be_visible()
            if username == 'author':
                page.screenshot(path=str(out/'login-desktop.png'), full_page=True)
            page.get_by_label('Имя пользователя').fill(username)
            page.get_by_label('Пароль', exact=True).fill(password)
            page.get_by_role('button', name='Войти в пространство').click()
            page.wait_for_url(base+'/')
            page.wait_for_load_state('networkidle')
            return page

        author = login('author')
        author.goto(base+'/requests/new/')
        author.locator('#id_request_type').select_option('SUGGESTION')
        author.locator('#id_category').select_option(label='Коммуникации')
        description = 'Проверка браузером: единый порядок фиксации решений сотрудников.'
        author.locator('#id_description').fill(description)
        author.locator('#id_proposed_solution').fill('Проверяемая демонстрационная инициатива.')
        author.get_by_role('button', name='Зарегистрировать обращение').click()
        author.wait_for_url('**/requests/*/')
        expect(author.get_by_role('heading', name=description, exact=True)).to_be_visible()
        ticket_url = author.url
        checks.append('author registration with CSRF and redirect')

        processor = login('processor')
        processor.screenshot(path=str(out/'inbox-desktop.png'), full_page=True)
        processor.goto(ticket_url)
        processor.get_by_role('button', name='Начать рассмотрение', exact=True).click()
        expect(processor.locator('.detail-heading .status-badge')).to_have_text('На рассмотрении')
        processor.locator('#comment-message').fill('INTERNAL-BROWSER-CHECK-9843')
        processor.locator('select[name=visibility]').select_option('internal')
        processor.get_by_role('button', name='Отправить', exact=True).click()
        author.reload()
        expect(author.get_by_text('INTERNAL-BROWSER-CHECK-9843')).to_have_count(0)
        checks.append('internal comment absent in author DOM')

        processor.get_by_role('button', name='Запросить уточнение', exact=True).click()
        processor.locator('#info-question').fill('Какой результат ожидается от изменения?')
        processor.get_by_role('button', name='Отправить запрос', exact=True).click()
        expect(processor.locator('.detail-heading .status-badge')).to_have_text('Ожидает уточнения')
        author.reload()
        author.locator('#info-answer').fill('Результат: решение доступно всем участникам процесса.')
        author.get_by_role('button', name='Отправить уточнение', exact=True).click()
        expect(author.locator('.detail-heading .status-badge')).to_have_text('На рассмотрении')
        checks.append('information request and author response')

        processor.reload()
        processor.get_by_role('button', name='Одобрить', exact=True).click()
        processor.locator('#approve-reason').fill('Провести демонстрационное улучшение')
        processor.locator('#approve-criteria').fill('Решение доступно участникам процесса')
        processor.locator('#approve-due').fill((datetime.now()+timedelta(days=10)).strftime('%Y-%m-%dT%H:%M'))
        processor.get_by_role('button', name='Одобрить и начать работу').click()
        expect(processor.locator('.detail-heading .status-badge')).to_have_text('В работе')
        processor.get_by_role('button', name='Подтвердить выполнение', exact=True).click()
        processor.locator('#implementation-result').fill('Демонстрационный порядок описан')
        processor.locator('#implementation-evidence').fill('Проверен тестовый артефакт по критерию готовности')
        processor.get_by_role('button', name='Зафиксировать выполнение', exact=True).click()
        expect(processor.locator('.detail-heading .status-badge')).to_have_text('Закрыто')
        processor.screenshot(path=str(out/'detail-desktop.png'), full_page=True)
        checks.append('approval and evidenced implementation')

        author.reload()
        author.locator('#reopen-reason').fill('Нужна проверка дополнительного сценария')
        author.get_by_role('button', name='Открыть повторно', exact=True).click()
        expect(author.locator('.detail-heading .status-badge')).to_have_text('На рассмотрении')
        author.locator('#history > summary').click()
        expect(author.get_by_text('Реализация завершена с подтверждённым результатом', exact=True)).to_be_visible()
        checks.append('reopening preserves prior history')

        processor.goto(base+'/analytics/?mode=production')
        expect(processor.get_by_text('Рабочие обращения', exact=True)).to_be_visible()
        expect(processor.locator('.overview > div').first.locator('strong')).to_have_text('0')
        processor.goto(base+'/analytics/?mode=demo')
        metric_total = int(processor.locator('.overview > div').first.locator('strong').inner_text())
        status_sum = sum(int(value) for value in processor.locator('.bar-chart').first.locator('.bar-row strong').all_text_contents())
        type_sum = sum(int(value) for value in processor.locator('.type-breakdown strong').all_text_contents())
        assert metric_total == status_sum == type_sum, (metric_total, status_sum, type_sum)
        processor.screenshot(path=str(out/'analytics-desktop.png'), full_page=True)
        checks.append('demo excluded from production metrics')
        checks.append('status and type totals match visible tickets')

        other = login('author2')
        response = other.goto(ticket_url)
        assert response.status == 404, response.status
        checks.append('foreign author receives 404')

        mobile = login('coordinator', mobile=True)
        expect(mobile.get_by_role('button', name='Открыть меню')).to_be_visible()
        mobile.get_by_role('button', name='Открыть меню').click()
        expect(mobile.locator('#sidebar')).to_be_visible()
        mobile.keyboard.press('Escape')
        mobile.screenshot(path=str(out/'inbox-mobile.png'), full_page=True)
        width = mobile.evaluate('({vw: innerWidth, width: document.documentElement.scrollWidth})')
        assert width['width'] <= width['vw'] + 1, width
        checks.append('mobile menu, escape and viewport fit')

        for context in contexts:
            context.close()
        browser.close()
    if errors:
        raise AssertionError(errors)
    result = {'checks': checks, 'javascript_errors': errors, 'ticket_url': ticket_url, 'screenshots': [f.name for f in out.glob('*.png')]}
    (out/'browser-report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': len(checks), 'javascript_errors': 0}))


if __name__ == '__main__':
    main()
