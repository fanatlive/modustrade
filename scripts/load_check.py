"""Guarded HTTP benchmark. Creates and drops only a fresh *_load database."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import math
import os
from pathlib import Path
import platform
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import sysconfig
import threading
import time
from urllib.parse import urlencode
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="Fresh database name, e.g. modus_20261111_load")
    parser.add_argument("--cards", type=int, default=10_000)
    parser.add_argument("--events", type=int, default=100_000)
    parser.add_argument("--rps", type=int, default=20)
    parser.add_argument("--duration", type=int, default=60, help="Seconds; full target is 1800")
    parser.add_argument("--sessions", type=int, default=100)
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--report", type=Path, default=ROOT / ".local/load/latest.json")
    parser.add_argument("--keep-database", action="store_true", help="Keep this newly created synthetic database for inspection")
    parser.add_argument("--stop-file", type=Path, help="Stop gracefully when this .local/load file appears; default <report>.stop")
    parser.add_argument("--fail-fast-after", type=int, default=60, help="Abort after this many seconds if the observed error rate exceeds 0.5%%; 0 disables")
    args = parser.parse_args()
    if not re.fullmatch(r"modus_[a-z0-9_]{1,40}_load", args.database):
        parser.error("database must be a fresh modus_<identifier>_load name")
    if not (100 <= args.cards <= 1_000_000 and args.cards <= args.events <= 10_000_000):
        parser.error("100..1000000 cards and cards..10000000 events required")
    if not (1 <= args.duration <= 7200 and 1 <= args.rps <= 200 and 1 <= args.sessions <= 100):
        parser.error("duration 1..7200, rps 1..200, sessions 1..100 required")
    args.report = args.report.resolve()
    # Reports may only be written inside the repository's ignored benchmark area.
    if not args.report.is_relative_to(ROOT / ".local/load"):
        parser.error("report must be inside .local/load")
    args.stop_file = (args.stop_file or args.report.with_suffix(".stop")).resolve()
    if not args.stop_file.is_relative_to(ROOT / ".local/load"):
        parser.error("stop-file must be inside .local/load")
    if args.stop_file.exists():
        parser.error("stop-file already exists; inspect and remove your old control file before a new run")
    if args.fail_fast_after < 0:
        parser.error("fail-fast-after must be nonnegative")
    return args


def load_environment(args):
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    os.environ.update(DEBUG="1", DEMO_MODE="1", DB_ENGINE="postgres", POSTGRES_DB=args.database,
                      DJANGO_SETTINGS_MODULE="config.settings", PYTHONIOENCODING="utf-8")


def percentile(values, quantile):
    return round(sorted(values)[max(0, math.ceil(len(values) * quantile) - 1)] * 1000, 2) if values else None


def prepare(args, run_id):
    import django
    django.setup()
    from django.conf import settings
    from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY, BACKEND_SESSION_KEY
    from django.contrib.sessions.backends.db import SessionStore
    from django.core.management import call_command
    from django.middleware.csrf import _get_new_csrf_string
    from django.utils import timezone
    from feedback.models import Category, Event, ProcessingCycle, Queue, Ticket, User

    call_command("migrate", interactive=False, verbosity=0)
    # No default passwords: authentication sessions are generated solely inside
    # this owned test database; login throttling is verified by separate tests.
    users = []
    for index in range(min(10, args.sessions)):
        user = User.objects.create_user(f"load-processor-{index}", role="processor")
        user.set_unusable_password()
        user.save(update_fields=["password"])
        users.append(user)
    author = User.objects.create_user("load-author")
    work_queue = Queue.objects.create(code="default", name="Synthetic load queue", owner=users[0])
    work_queue.members.add(*users)
    categories = Category.objects.bulk_create([Category(code=f"load-{i}", name=f"Тестовая категория {i}") for i in range(20)])
    now = timezone.now()
    targets = []
    remaining_events = args.events
    for start in range(0, args.cards, 500):
        cards = []
        for i in range(start, min(start + 500, args.cards)):
            closed = i >= args.sessions and i % 4 == 0
            cards.append(Ticket(
                number=f"LOAD-{i:010}", author=author, category=categories[i % len(categories)], queue=work_queue,
                assignee=users[i % len(users)], request_type="PROBLEM", status="CLOSED" if closed else "REVIEW",
                description=f"Синтетическая проверка каталога оборудования и согласования документов. Позиция {i}. " + "Контрольный текст обращения. " * 8,
                created_at=now, submitted_at=now, is_demo=True,
                resolution_code="DECLINED" if closed else "", resolution_summary="Тестовый результат" if closed else "",
                closed_at=now if closed else None, source_data={"origin": "guarded_load", "run_id": run_id},
            ))
        Ticket.objects.bulk_create(cards, batch_size=500)
        cycles, events = [], []
        for offset, card in enumerate(cards):
            i = start + offset
            if card.status != "CLOSED" and len(targets) < args.sessions:
                targets.append(str(card.pk))
            cycles.append(ProcessingCycle(ticket=card, number=1, started_at=now,
                closed_at=card.closed_at, resolution_code=card.resolution_code, resolution_summary=card.resolution_summary,
                first_response_at=now, decision_at=now if card.status == "CLOSED" else None,
                policy={"benchmark": True, "business_timezone": settings.BUSINESS_TIMEZONE}))
            count = args.events // args.cards + (1 if i < args.events % args.cards else 0)
            for sequence in range(1, count + 1):
                events.append(Event(ticket=card, actor=users[i % len(users)], kind="LOAD_HISTORY",
                    summary="Синтетическое событие для проверки чтения истории.", visibility="public",
                    sequence=sequence, origin="import", payload={"run_id": run_id, "cycle_no": 1}))
            card.version = count
            remaining_events -= count
        ProcessingCycle.objects.bulk_create(cycles, batch_size=500)
        Event.objects.bulk_create(events, batch_size=1000)
        Ticket.objects.bulk_update(cards, ["version"], batch_size=500)
    assert remaining_events == 0
    # A client owns its mutation card, preventing benchmark-generated version
    # conflicts while still executing the real authorization and write path.
    clients = queue.Queue()
    targets = list(Ticket.objects.filter(pk__in=targets).values_list("pk", "version"))
    for index in range(args.sessions):
        user = users[index % len(users)]
        session = SessionStore()
        session[SESSION_KEY] = str(user.pk)
        session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
        session[HASH_SESSION_KEY] = user.get_session_auth_hash()
        session.save()
        csrf = _get_new_csrf_string()
        card, version = targets[index % len(targets)]
        clients.put({"cookie": f"{settings.SESSION_COOKIE_NAME}={session.session_key}; {settings.CSRF_COOKIE_NAME}={csrf}",
                     "csrf": csrf, "ticket": str(card), "version": version})
    return clients, [str(pk) for pk in Ticket.objects.values_list("pk", flat=True)]


def main():
    args = arguments()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    load_environment(args)
    free = shutil.disk_usage(ROOT).free
    estimated = args.cards * 12_288 + args.events * 4096
    if free < estimated + 1024 ** 3:
        guard = {"phase": "disk_guard", "accepted": False, "database_created": False,
                 "database": args.database, "cards": args.cards, "events": args.events,
                 "disk_free_bytes": free, "estimated_required_bytes": estimated + 1024 ** 3,
                 "full_scale_target_verified": False,
                 "reason": "Insufficient free disk for guarded synthetic preparation."}
        args.report.write_text(json.dumps(guard, indent=2) + "\n", encoding="utf-8")
        raise SystemExit(f"Refusing preparation: {free // 1024**2} MiB free; guarded estimate requires {(estimated + 1024**3) // 1024**2} MiB. No database created.")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", args.port))
        except OSError as exc:
            raise SystemExit("Benchmark port is already in use; no database created.") from exc
    import psycopg
    from psycopg import sql
    admin_params = {"dbname": "postgres", "host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
                    "port": os.getenv("POSTGRES_PORT", "55432"), "user": os.getenv("POSTGRES_USER", "modus"),
                    "password": os.getenv("POSTGRES_PASSWORD", ""), "connect_timeout": 5}
    # settings normally defaults to port5432; align the child with this explicit
    # driver default when the caller did not choose another PostgreSQL endpoint.
    os.environ.setdefault("POSTGRES_PORT", str(admin_params["port"]))
    created = False
    server = None
    report = {"run_id": run_id, "database": args.database, "synthetic_only": True,
              "cards": args.cards, "events": args.events, "sessions": args.sessions,
              "rps_target": args.rps, "duration_target_seconds": args.duration,
              "mix": {"read": 0.7, "search": 0.2, "write": 0.1},
              "stop_file": str(args.stop_file), "fail_fast_after_seconds": args.fail_fast_after,
              "environment": {"platform": platform.platform(), "python": platform.python_version(),
                              "logical_cpus": os.cpu_count(), "disk_free_before_bytes": free},
              "full_scale_target_verified": False}
    started_prepare = time.monotonic()
    try:
        with psycopg.connect(**admin_params, autocommit=True) as admin:
            if admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", [args.database]).fetchone():
                raise SystemExit("Database already exists. Refusing to modify any existing database.")
            admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(args.database)))
            created = True
        clients, cards = prepare(args, run_id)
        from feedback.models import User
        from feedback.permissions import visible_tickets
        from feedback.search import scoped_search
        with psycopg.connect(**{**admin_params, "dbname": args.database}, autocommit=True) as db:
            db.execute("CREATE TABLE guarded_load_marker (run_id text PRIMARY KEY)")
            db.execute("INSERT INTO guarded_load_marker VALUES (%s)", [run_id])
            db.execute("ANALYZE")
            report["database_bytes_prepared"] = db.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        processor = User.objects.get(username="load-processor-0")
        report["search_explain_analyze_buffers"] = json.loads(scoped_search(
            visible_tickets(processor), "оборудования").explain(analyze=True, buffers=True, format="json"))
        report["prepare_seconds"] = round(time.monotonic() - started_prepare, 2)
        log_path = args.report.parent / f"{run_id}.server.log"
        with log_path.open("wb") as log:
            # Windows venv python.exe is a launcher that can leave its child
            # alive after terminate(). Run the real interpreter directly with
            # this environment's site-packages to own exactly one server PID.
            bootstrap = "import site,runpy;site.addsitedir(" + repr(sysconfig.get_path("purelib")) + ");runpy.run_module('waitress',run_name='__main__')"
            server = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c", bootstrap, "--threads=8",
                f"--listen=127.0.0.1:{args.port}", "config.wsgi:application"], cwd=ROOT,
                env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        for _ in range(100):
            if server.poll() is not None:
                raise RuntimeError("Owned benchmark server failed; inspect its private .local/load log.")
            try:
                check = http.client.HTTPConnection("127.0.0.1", args.port, timeout=1)
                check.request("GET", "/health/")
                ready = check.getresponse().status == 200
                check.close()
                if ready:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Owned benchmark server readiness timed out.")
        stats = {kind: {"latencies": [], "status": Counter(), "errors": 0} for kind in ("read", "search", "write")}
        lock = threading.Lock()
        dropped = 0
        randomizer = random.Random(20261111)
        actions = ["read"] * 7 + ["search"] * 2 + ["write"]
        total_bytes = 0

        def request(kind, ordinal, scheduled):
            nonlocal total_bytes
            client = clients.get()
            status = 0
            body_size = 0
            try:
                conn = http.client.HTTPConnection("127.0.0.1", args.port, timeout=15)
                headers = {"Cookie": client["cookie"], "Host": f"127.0.0.1:{args.port}"}
                if kind == "write":
                    path = f"/requests/{client['ticket']}/command/"
                    body = urlencode({"command": "comment", "version": client["version"], "visibility": "public",
                                      "message": f"Синтетический комментарий нагрузки {ordinal} {run_id}"})
                    headers.update({"Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": client["csrf"]})
                    conn.request("POST", path, body=body, headers=headers)
                elif kind == "search":
                    conn.request("GET", "/?" + urlencode({"q": "оборудования" if ordinal % 2 else "документов"}), headers=headers)
                else:
                    path = "/" if ordinal % 10 == 0 else f"/requests/{cards[ordinal % len(cards)]}/"
                    conn.request("GET", path, headers=headers)
                response = conn.getresponse()
                status = response.status
                body_size = len(response.read())
                conn.close()
                if kind == "write" and status == 302:
                    client["version"] += 1
            except (OSError, http.client.HTTPException):
                status = 0
            finally:
                elapsed = time.monotonic() - scheduled
                with lock:
                    item = stats[kind]
                    item["latencies"].append(elapsed)
                    item["status"][str(status)] += 1
                    item["errors"] += int(status != (302 if kind == "write" else 200))
                    total_bytes += body_size
                clients.put(client)

        start = time.monotonic()
        next_progress = start + 30
        planned = args.duration * args.rps
        scheduled_count = 0
        abort_reason = None
        futures = set()
        with ThreadPoolExecutor(max_workers=args.sessions) as executor:
            for ordinal in range(planned):
                scheduled = start + ordinal / args.rps
                time.sleep(max(0, scheduled - time.monotonic()))
                if args.stop_file.exists():
                    abort_reason = "operator_stop_file"
                    break
                with lock:
                    error_count = dropped + sum(item["errors"] for item in stats.values())
                if args.fail_fast_after and time.monotonic() - start >= args.fail_fast_after and error_count / max(scheduled_count, 1) > .005:
                    abort_reason = "observed_error_rate_exceeded"
                    break
                scheduled_count += 1
                if ordinal % 10 == 0:
                    randomizer.shuffle(actions)
                kind = actions[ordinal % 10]
                futures = {future for future in futures if not future.done()}
                if len(futures) >= args.sessions:
                    dropped += 1
                else:
                    futures.add(executor.submit(request, kind, ordinal, scheduled))
                if time.monotonic() >= next_progress:
                    print(json.dumps({"elapsed_seconds": round(time.monotonic() - start), "scheduled": ordinal + 1,
                                      "dropped": dropped, "errors": sum(item["errors"] for item in stats.values())}), flush=True)
                    report.update(phase="running", scheduled_requests=scheduled_count,
                        actual_duration_seconds=round(time.monotonic()-start,2), dropped_requests=dropped,
                        observed_errors=sum(item["errors"] for item in stats.values()), results_provisional=True)
                    with lock:
                        report["results"] = {name: {"count": len(item["latencies"]), "errors": item["errors"],
                            "statuses": dict(item["status"]), "p95_ms": percentile(item["latencies"], .95)}
                            for name, item in stats.items()}
                    snapshot = args.report.with_suffix(".json.tmp")
                    snapshot.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
                    snapshot.replace(args.report)
                    next_progress += 30
            for future in futures:
                future.result()
        actual_duration = time.monotonic() - start
        results = {}
        for kind, item in stats.items():
            results[kind] = {"count": len(item["latencies"]), "errors": item["errors"], "statuses": dict(item["status"]),
                             "p50_ms": percentile(item["latencies"], .5), "p95_ms": percentile(item["latencies"], .95),
                             "p99_ms": percentile(item["latencies"], .99), "max_ms": round(max(item["latencies"], default=0) * 1000, 2)}
        errors = dropped + sum(item["errors"] for item in stats.values())
        report.update(results=results, planned_requests_target=planned, scheduled_requests=scheduled_count, dropped_requests=dropped,
            phase="aborted" if abort_reason else "completed", aborted=bool(abort_reason), abort_reason=abort_reason, results_provisional=False,
            actual_duration_seconds=round(actual_duration, 2), completed_rps=round((scheduled_count-dropped)/actual_duration, 2),
            error_rate=round(errors/max(scheduled_count,1), 6), response_bytes=total_bytes,
            limits_met=not abort_reason and errors/max(scheduled_count,1) <= .005 and all(results[kind]["p95_ms"] is not None and results[kind]["p95_ms"] <= limit
                for kind, limit in (("read",1000),("search",2000),("write",1500))))
        report["full_scale_target_verified"] = (args.cards == 1_000_000 and args.events == 10_000_000
            and args.duration >= 1800 and args.rps >= 20 and args.sessions >= 100 and report["limits_met"])
    except BaseException as exc:
        report["failure"] = type(exc).__name__
        raise
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        if created and not args.keep_database:
            from django.db import connections
            connections.close_all()
            with psycopg.connect(**admin_params, autocommit=True) as admin:
                # This name was absent at start and created by this invocation.
                # Never accept an arbitrary target database for cleanup.
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(args.database)))
        report["database_retained"] = created and args.keep_database
        report["disk_free_after_bytes"] = shutil.disk_usage(ROOT).free
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"report": str(args.report), "limits_met": report.get("limits_met"),
                          "database_retained": report["database_retained"]}), flush=True)
    return 0 if report.get("limits_met") else 1


if __name__ == "__main__":
    raise SystemExit(main())
