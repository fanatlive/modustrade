"""Read-only HTTPS/configuration probe of the isolated local production profile."""
import argparse
import json
from pathlib import Path
import re
import ssl
import subprocess
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', default='.local/probe.production.env')
    parser.add_argument('--certificate', default='.local/tls/server.crt')
    parser.add_argument('--report', default='test-results/production-probe.json')
    args = parser.parse_args()
    values = {}
    for line in Path(args.env_file).read_text(encoding='utf-8').splitlines():
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values[key] = value
    if not values.get('POSTGRES_DB', '').endswith('_probe'):
        parser.error('This probe requires a dedicated database ending in _probe.')
    base = 'https://localhost:'+values.get('HTTPS_PORT', '8443')
    context = ssl.create_default_context(cafile=args.certificate)

    def get(path, token=None):
        request = Request(base+path, headers={'Authorization': 'Bearer '+token} if token else {})
        try:
            with urlopen(request, context=context, timeout=10) as response:
                return response.status, response.headers, response.read()
        except HTTPError as response:
            return response.code, response.headers, response.read()

    checks = []
    for path in ('/health/', '/health/worker/'):
        status, _, _ = get(path)
        assert status == 200, (path, status)
    checks.append('TLS certificate verified; app and worker ready')
    status, headers, body = get('/accounts/login/')
    assert status == 200
    assert headers.get('X-Frame-Options') == 'DENY'
    assert headers.get('Strict-Transport-Security', '').startswith('max-age=31536000')
    cookie = headers.get('Set-Cookie', '')
    assert 'Secure' in cookie and 'SameSite=Lax' in cookie
    checks.append('Production security headers and secure CSRF cookie')
    stylesheet = re.search(rb'href="(/static/[^"?]+\.css)"', body)
    assert stylesheet and get(stylesheet[1].decode())[0] == 200
    checks.append('Prebuilt versioned static CSS served read-only')
    assert get('/operations/metrics/')[0] == 404
    status, _, payload = get('/operations/metrics/', values['OPERATIONS_TOKEN'])
    assert status == 200 and json.loads(payload)['worker_healthy']
    assert get('/operations/metrics/', '\u00e9')[0] == 404
    checks.append('Monitoring token required; malformed token fails safely')
    compose = ['docker', 'compose', '--env-file', args.env_file, '-f', 'compose.production.yaml']
    canary = 'AUDIT_PRIVATE_QUERY_'+uuid.uuid4().hex
    assert get('/accounts/login/?q='+canary)[0] == 200
    logs = subprocess.run(compose+['logs', '--no-color', '--tail', '100', 'proxy'], capture_output=True, text=True)
    assert logs.returncode == 0 and canary not in logs.stdout+logs.stderr
    checks.append('Proxy access logs omit query text')
    code = """import os,django,json; os.environ['DJANGO_SETTINGS_MODULE']='config.settings'; django.setup()
from django.conf import settings
from django.db import connection
with connection.cursor() as cursor:
 cursor.execute("SELECT current_user,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls,has_schema_privilege(current_user,'public','CREATE') FROM pg_roles WHERE rolname=current_user")
 row=cursor.fetchone()
assert row[0]==settings.DATABASES['default']['USER'] and not any(row[1:]),row
print(json.dumps({'runtime_restricted':True,'debug':settings.DEBUG,'demo_mode':settings.DEMO_MODE}))
"""
    result = subprocess.run(compose+['exec', '-T', 'web', 'python', '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-1000:]
    runtime = json.loads(result.stdout)
    assert runtime['runtime_restricted'] and not runtime['debug'] and not runtime['demo_mode']
    checks.append('Runtime role has no superuser, create DB/role/schema or bypass RLS')
    result = subprocess.run(compose+['run', '--rm', '--no-deps', 'migrate', 'python', 'manage.py', 'check', '--deploy'],
                            capture_output=True, text=True)
    assert result.returncode == 0 and 'identified no issues' in result.stdout, result.stderr[-1500:]
    checks.append('Django deployment checks passed')
    report = Path(args.report)
    report.parent.mkdir(exist_ok=True, parents=True)
    report.write_text(json.dumps({'checks': checks, 'passed': len(checks)}, indent=2), encoding='utf-8')
    print(json.dumps({'passed': len(checks)}))


if __name__ == '__main__':
    main()
