"""Own one local QA server process and terminate only its process handle."""
import os
from pathlib import Path
import site
import socket
import subprocess
import sys
import time
from urllib.request import urlopen


def main():
    postgres = os.getenv("DB_ENGINE") == "postgres" and os.getenv("POSTGRES_DB", "").endswith("_browser")
    sqlite_path = Path(os.getenv("SQLITE_PATH", ""))
    sqlite = os.getenv("DB_ENGINE") == "sqlite" and sqlite_path.is_absolute() and sqlite_path.name.endswith("_browser.sqlite3")
    if not (postgres or sqlite):
        raise SystemExit("Use PostgreSQL *_browser or an absolute SQLite *_browser.sqlite3 QA database.")
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8765)) == 0:
            raise SystemExit("QA port 8765 is already in use.")
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DEBUG": "1", "DEMO_MODE": "1", "PYTHONIOENCODING": "utf-8"}
    env["PYTHONPATH"] = os.pathsep.join([*site.getsitepackages(), str(root)])
    output = root / "test-results"
    output.mkdir(exist_ok=True)
    subprocess.run([sys.executable, "manage.py", "load_hackathon_demo"], cwd=root, env=env, check=True)
    with (output / "browser-owned-server.log").open("wb") as log:
        # Starting the base executable directly avoids Windows venv launcher
        # children. Popen owns exactly the server process being cleaned up.
        server = subprocess.Popen([sys._base_executable, "manage.py", "runserver", "127.0.0.1:8765", "--noreload"],
                                  cwd=root, env=env, stdout=log, stderr=log,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            for _ in range(30):
                if server.poll() is not None:
                    raise RuntimeError("Owned QA server exited; inspect browser-owned-server.log")
                try:
                    with urlopen("http://127.0.0.1:8765/health/", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(1)
            else:
                raise RuntimeError("QA server did not become ready")
            for script in ("scripts/browser_smoke.py", "tests/browser_ui_remediation.py", "tests/browser_recurrence.py", "tests/browser_presentation.py"):
                subprocess.run([sys.executable, script, "--base-url", "http://127.0.0.1:8765"], cwd=root, env=env, check=True)
        finally:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
    print("All four browser suites passed; owned QA server stopped.")


if __name__ == "__main__":
    main()
