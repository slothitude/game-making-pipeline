#!/usr/bin/env python3
"""GMP job queue API — the one door every work-order walks through.

stdlib-only HTTP server (http.server). Token auth via X-Token header.
Backends behind the same function names:
  --backend pg     production (retromonkey Postgres, gmp/pipeline)
  --backend sqlite local smoke test (cloud/queue/test.db)

Endpoints:
  POST /jobs              {type, payload, priority}      -> {id}
  GET  /jobs?status=&limit=                              -> {jobs:[...]}
  POST /jobs/claim       {types:[...]}                   -> {job: {...}|null}
  POST /jobs/<id>/result {result}|{error}                -> {id, status}
  DELETE /jobs/<id>      (or POST /jobs/<id>/cancel)     -> {id, status: cancelled}
                          queued ONLY — running jobs go through the result
                          route (the coordinator's race-safe law)
  GET  /health                                           -> {ok, backend, counts}
"""

import argparse
import json
import os
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"
PORT = 8901
QUEUE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(QUEUE_DIR, "test.db")
MAX_ATTEMPTS = 2  # the retry law: a job that errors twice is failed permanently

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def say(msg):
    """No-silent-jobs law: one line per event, always."""
    print(f"[queue_api] {msg}", flush=True)


# --------------------------------------------------------------------------
# SQLite backend (smoke tests, runs anywhere)
# --------------------------------------------------------------------------

class SQLiteBackend:
    name = "sqlite"

    def __init__(self, path):
        self.path = path

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def init(self):
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    type        TEXT NOT NULL,
                    payload     TEXT NOT NULL DEFAULT '{}',
                    status      TEXT NOT NULL DEFAULT 'queued',
                    priority    INTEGER DEFAULT 5,
                    result      TEXT,
                    error       TEXT,
                    attempts    INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT DEFAULT (datetime('now')),
                    started_at  TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_claim_idx
                    ON jobs (status, priority, created_at);
                """
            )
        say(f"backend=sqlite ready at {self.path}")

    @staticmethod
    def _row_to_job(row):
        if row is None:
            return None
        job = dict(row)
        for field in ("payload", "result"):
            if job.get(field) is not None and not isinstance(job[field], dict):
                try:
                    job[field] = json.loads(job[field])
                except (TypeError, ValueError):
                    pass
        return job

    def enqueue(self, jtype, payload, priority):
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO jobs (type, payload, priority) VALUES (?, ?, ?)",
                (jtype, json.dumps(payload), priority),
            )
            return cur.lastrowid

    def list_jobs(self, status, limit):
        sql = "SELECT * FROM jobs"
        params = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_job(r) for r in rows]

    def claim(self, types):
        """Atomic pop: one highest-priority oldest queued job -> running."""
        placeholders = ",".join("?" for _ in types)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")  # sqlite's SKIP LOCKED
            cur = conn.execute(
                f"""
                UPDATE jobs SET status = 'running', started_at = datetime('now')
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE status = 'queued' AND type IN ({placeholders})
                    ORDER BY priority ASC, created_at ASC, id ASC
                    LIMIT 1
                )
                RETURNING id, type, payload, priority, status, attempts, created_at
                """,
                types,
            )
            row = cur.fetchone()
        return self._row_to_job(row)

    def complete(self, job_id, result):
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE jobs SET status = 'done', result = ?,
                       finished_at = datetime('now')
                WHERE id = ? AND status IN ('running', 'queued')
                """,
                (json.dumps(result), job_id),
            )
            return cur.rowcount > 0

    def fail(self, job_id, error, permanent=False):
        """Retry law: error #1 -> back to queued; error #2 -> failed forever.
        permanent=True skips the retry (a not-wired executor never wires itself)."""
        with self._conn() as conn:
            if permanent:
                cur = conn.execute(
                    """
                    UPDATE jobs SET attempts = attempts + 1, status = 'failed',
                           error = ?, finished_at = datetime('now')
                    WHERE id = ? AND status IN ('running', 'queued')
                    """,
                    (error, job_id),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE jobs SET
                        attempts = attempts + 1,
                        status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'queued' END,
                        error = ?,
                        finished_at = CASE WHEN attempts + 1 >= ?
                                           THEN datetime('now') ELSE finished_at END
                    WHERE id = ? AND status IN ('running', 'queued')
                    """,
                    (MAX_ATTEMPTS, error, MAX_ATTEMPTS, job_id),
                )
            if cur.rowcount <= 0:
                return None
            row = conn.execute(
                "SELECT status, attempts FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return dict(row)

    def counts(self):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def cancel(self, job_id):
        """Delete a QUEUED job. 'deleted' | 'not_found' | 'not_queued'.
        The DELETE carries its own status guard, so a claim racing the cancel
        wins and the cancel reports not_queued — never a vanished running job."""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM jobs WHERE id = ? AND status = 'queued'", (job_id,))
            if cur.rowcount > 0:
                return "deleted"
            row = conn.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return "not_found" if row is None else "not_queued"


# --------------------------------------------------------------------------
# Postgres backend (production — retromonkey)
# --------------------------------------------------------------------------

class PGBackend:
    name = "pg"

    def __init__(self):
        import psycopg2  # the only non-stdlib import, and only on this path

        self.psycopg2 = psycopg2
        self.dsn_kwargs = dict(
            host=os.environ.get("PGHOST", "localhost"),
            dbname=os.environ.get("PGDATABASE", "gmp"),
            user=os.environ.get("PGUSER", "pipeline"),
            password=os.environ.get("PGPASSWORD", "slothitude2026"),
        )

    def _conn(self):
        return self.psycopg2.connect(**self.dsn_kwargs)

    def init(self):
        schema_path = os.path.join(QUEUE_DIR, "schema.sql")
        with open(schema_path, "r", encoding="utf-8") as fh:
            schema = fh.read()
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(schema)  # psycopg2 runs multi-statement scripts fine
        say(
            "backend=pg ready at {host}/{db} as {user} (schema.sql applied)".format(
                host=self.dsn_kwargs["host"],
                db=self.dsn_kwargs["dbname"],
                user=self.dsn_kwargs["user"],
            )
        )

    @staticmethod
    def _row_to_job(cur, row):
        if row is None:
            return None
        cols = [d.name for d in cur.description]
        return dict(zip(cols, row))

    def enqueue(self, jtype, payload, priority):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO jobs (type, payload, priority) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (jtype, json.dumps(payload), priority),
                )
                return cur.fetchone()[0]

    def list_jobs(self, status, limit):
        sql = "SELECT id, type, payload, status, priority, result, error, attempts, "
        sql += "created_at, started_at, finished_at FROM jobs"
        params = []
        if status:
            sql += " WHERE status = %s"
            params.append(status)
        sql += " ORDER BY created_at DESC, id DESC LIMIT %s"
        params.append(limit)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return [self._row_to_job(cur, r) for r in cur.fetchall()]

    def claim(self, types):
        """Atomic pop — FOR UPDATE SKIP LOCKED, safe with many routers."""
        sql = """
            UPDATE jobs SET status = 'running', started_at = now()
            WHERE id = (
                SELECT id FROM jobs
                WHERE status = 'queued' AND type = ANY(%s)
                ORDER BY priority ASC, created_at ASC, id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, type, payload, priority, status, attempts, created_at
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (types,))
                row = cur.fetchone()
                return self._row_to_job(cur, row)

    def complete(self, job_id, result):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jobs SET status = 'done', result = %s,
                           finished_at = now()
                    WHERE id = %s AND status IN ('running', 'queued')
                    RETURNING id
                    """,
                    (json.dumps(result), job_id),
                )
                return cur.fetchone() is not None

    def fail(self, job_id, error, permanent=False):
        """Retry law: error #1 -> back to queued; error #2 -> failed forever.
        permanent=True skips the retry (a not-wired executor never wires itself)."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                if permanent:
                    cur.execute(
                        """
                        UPDATE jobs SET attempts = attempts + 1,
                               status = 'failed', error = %s, finished_at = now()
                        WHERE id = %s AND status IN ('running', 'queued')
                        RETURNING attempts
                        """,
                        (error, job_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE jobs SET
                            attempts = attempts + 1,
                            status = CASE WHEN attempts + 1 >= %s
                                          THEN 'failed' ELSE 'queued' END,
                            error = %s,
                            finished_at = CASE WHEN attempts + 1 >= %s
                                               THEN now() ELSE finished_at END
                        WHERE id = %s AND status IN ('running', 'queued')
                        RETURNING status, attempts
                        """,
                        (MAX_ATTEMPTS, error, MAX_ATTEMPTS, job_id),
                    )
                row = cur.fetchone()
                if row is None:
                    return None
                if permanent:
                    return {"status": "failed", "attempts": row[0]}
                return {"status": row[0], "attempts": row[1]}

    def counts(self):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")
                return {status: n for status, n in cur.fetchall()}

    def cancel(self, job_id):
        """Delete a QUEUED job. 'deleted' | 'not_found' | 'not_queued'.
        The DELETE carries its own status guard, so a claim racing the cancel
        wins and the cancel reports not_queued — never a vanished running job."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM jobs WHERE id = %s AND status = 'queued'
                    RETURNING id
                    """,
                    (job_id,),
                )
                if cur.fetchone() is not None:
                    return "deleted"
                cur.execute("SELECT status FROM jobs WHERE id = %s", (job_id,))
                row = cur.fetchone()
        return "not_found" if row is None else "not_queued"


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

class QueueHandler(BaseHTTPRequestHandler):
    backend = None  # set in main()
    token = None

    def log_message(self, fmt, *args):  # narration is one line per handler
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return self.token and self.headers.get("X-Token", "") == self.token

    def _body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            obj = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None
        return obj if isinstance(obj, dict) else None

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            counts = self.backend.counts()
            say(f"GET /health -> ok, {counts}")
            self._json(200, {"ok": True, "backend": self.backend.name, "counts": counts})
            return

        if parsed.path == "/jobs":
            if not self._authed():
                say(f"GET /jobs -> 403 (bad token from {self.client_address[0]})")
                self._json(403, {"error": "bad X-Token"})
                return
            qs = parse_qs(parsed.query)
            status = (qs.get("status") or [None])[0]
            if status is not None and status not in ("queued", "running", "done", "failed"):
                say(f"GET /jobs -> 400 (unknown status {status!r})")
                self._json(400, {"error": "status must be queued|running|done|failed"})
                return
            try:
                limit = min(max(int((qs.get("limit") or ["50"])[0]), 1), 500)
            except ValueError:
                limit = 50
            jobs = self.backend.list_jobs(status, limit)
            say(f"GET /jobs status={status or 'any'} limit={limit} -> {len(jobs)} jobs")
            self._json(200, {"jobs": jobs, "count": len(jobs)})
            return

        say(f"GET {parsed.path} -> 404")
        self._json(404, {"error": "not found"})

    # -- DELETE ------------------------------------------------------------
    def _cancel(self, raw_id):
        """DELETE /jobs/<id> and POST /jobs/<id>/cancel share this: queued
        only. A running job must finish through the result route — deleting
        it here would orphan a worker mid-claim."""
        try:
            job_id = int(raw_id)
        except ValueError:
            say(f"cancel {raw_id!r} -> 400 (bad job id)")
            self._json(400, {"error": "bad job id"})
            return
        outcome = self.backend.cancel(job_id)
        if outcome == "deleted":
            say(f"DELETE /jobs/{job_id} -> cancelled (was queued)")
            self._json(200, {"id": job_id, "status": "cancelled"})
        elif outcome == "not_found":
            say(f"DELETE /jobs/{job_id} -> 404 (no such job)")
            self._json(404, {"error": "no such job"})
        else:
            say(f"DELETE /jobs/{job_id} -> 409 (not queued — running jobs go "
                f"through the result route)")
            self._json(409, {"error": "job not in queued"})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if not self._authed():
            say(f"DELETE {parsed.path} -> 403 (bad token from {self.client_address[0]})")
            self._json(403, {"error": "bad X-Token"})
            return
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "jobs":
            self._cancel(parts[1])
            return
        say(f"DELETE {parsed.path} -> 404")
        self._json(404, {"error": "not found"})

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._authed():
            say(f"POST {parsed.path} -> 403 (bad token from {self.client_address[0]})")
            self._json(403, {"error": "bad X-Token"})
            return

        body = self._body()
        if body is None:
            say(f"POST {parsed.path} -> 400 (body is not a JSON object)")
            self._json(400, {"error": "body must be a JSON object"})
            return

        if parsed.path == "/jobs":
            jtype = body.get("type")
            if not isinstance(jtype, str) or not jtype.strip():
                say("POST /jobs -> 400 (missing 'type')")
                self._json(400, {"error": "'type' is required"})
                return
            payload = body.get("payload") or {}
            if not isinstance(payload, dict):
                say("POST /jobs -> 400 ('payload' must be an object)")
                self._json(400, {"error": "'payload' must be an object"})
                return
            try:
                priority = int(body.get("priority", 5))
            except (TypeError, ValueError):
                say("POST /jobs -> 400 ('priority' must be an int)")
                self._json(400, {"error": "'priority' must be an int"})
                return
            priority = min(max(priority, 1), 99)
            job_id = self.backend.enqueue(jtype.strip(), payload, priority)
            say(f"POST /jobs type={jtype.strip()} priority={priority} -> id={job_id}")
            self._json(200, {"id": job_id})
            return

        if parsed.path == "/jobs/claim":
            types = body.get("types")
            if types is None:
                types = ["llm", "device_test", "gpu", "gate", "deploy", "emulator", "critique"]
            if not isinstance(types, list) or not types or not all(isinstance(t, str) for t in types):
                say("POST /jobs/claim -> 400 ('types' must be a list of strings)")
                self._json(400, {"error": "'types' must be a list of strings"})
                return
            job = self.backend.claim(types)
            if job is None:
                say(f"POST /jobs/claim types={types} -> nothing queued")
                self._json(200, {"job": None})
            else:
                say(f"POST /jobs/claim -> claimed id={job['id']} type={job['type']} "
                    f"priority={job['priority']}")
                self._json(200, {"job": job})
            return

        parts = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
            self._cancel(parts[1])
            return
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "result":
            try:
                job_id = int(parts[1])
            except ValueError:
                say(f"POST {parsed.path} -> 400 (bad job id)")
                self._json(400, {"error": "bad job id"})
                return
            if "result" in body:
                ok = self.backend.complete(job_id, body["result"])
                if ok:
                    say(f"POST /jobs/{job_id}/result -> done")
                    self._json(200, {"id": job_id, "status": "done"})
                else:
                    say(f"POST /jobs/{job_id}/result -> 409 (not claimable)")
                    self._json(409, {"error": "job not in running/queued"})
                return
            if "error" in body:
                permanent = bool(body.get("permanent"))
                outcome = self.backend.fail(job_id, str(body["error"])[:2000],
                                            permanent=permanent)
                if outcome is None:
                    say(f"POST /jobs/{job_id}/result -> 409 (not claimable)")
                    self._json(409, {"error": "job not in running/queued"})
                elif outcome["status"] == "failed":
                    say(f"POST /jobs/{job_id}/result -> FAILED permanently "
                        f"(attempt {outcome['attempts']}/{MAX_ATTEMPTS})"
                        + (" [permanent]" if permanent else ""))
                    self._json(200, {"id": job_id, "status": "failed",
                                     "attempts": outcome["attempts"]})
                else:
                    say(f"POST /jobs/{job_id}/result -> requeued for retry "
                        f"(attempt {outcome['attempts']}/{MAX_ATTEMPTS})")
                    self._json(200, {"id": job_id, "status": "queued",
                                     "attempts": outcome["attempts"]})
                return
            say(f"POST /jobs/{job_id}/result -> 400 (need 'result' or 'error')")
            self._json(400, {"error": "body needs 'result' or 'error'"})
            return

        say(f"POST {parsed.path} -> 404")
        self._json(404, {"error": "not found"})


# --------------------------------------------------------------------------
# selftest (offline — sqlite, no HTTP, no network)
# --------------------------------------------------------------------------

def _selftest():
    """The cancel law, against a throwaway sqlite db:
    queued -> deleted and gone; running/done/failed -> not_queued (the result
    route owns those); unknown id -> not_found."""
    import tempfile

    db = os.path.join(tempfile.mkdtemp(prefix="gmp_queue_selftest_"), "test.db")
    backend = SQLiteBackend(db)
    backend.init()

    jid = backend.enqueue("selftest", {"note": "throwaway"}, 5)
    assert backend.cancel(jid) == "deleted", "a queued job must cancel"
    assert backend.list_jobs("queued", 50) == [], "cancelled job must be gone"
    assert backend.cancel(jid) == "not_found", "a cancelled job is gone-gone"
    assert backend.cancel(999999) == "not_found", "unknown id -> not_found"
    say("selftest: queued -> deleted, gone-gone; unknown -> not_found")

    jid = backend.enqueue("selftest", {}, 5)
    claimed = backend.claim(["selftest"])
    assert claimed and claimed["id"] == jid, "claim must move it to running"
    assert backend.cancel(jid) == "not_queued", "running must refuse to cancel"
    running = backend.list_jobs("running", 50)
    assert [j["id"] for j in running] == [jid], "running job must survive"
    say("selftest: running -> not_queued (the result route owns it)")

    backend.complete(jid, {"ok": True})
    assert backend.cancel(jid) == "not_queued", "done history is not deletable"
    assert backend.list_jobs("done", 50)[0]["id"] == jid
    say("selftest: done -> not_queued (history stays)")

    # the race: claim wins over a concurrent cancel, the guard keeps the job
    jid = backend.enqueue("selftest", {}, 5)
    backend.claim(["selftest"])
    assert backend.cancel(jid) == "not_queued"
    backend.fail(jid, "selftest teardown", permanent=True)
    assert backend.cancel(jid) == "not_queued", "failed history stays too"
    say("selftest: cancel law ok — db " + db)
    return 0


def main():
    ap = argparse.ArgumentParser(description="GMP job queue API")
    ap.add_argument("--backend", choices=("pg", "sqlite"), default="pg",
                    help="pg = production Postgres, sqlite = local smoke test")
    ap.add_argument("--db", default=DEFAULT_DB, help="sqlite file (default test.db)")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--host", default=HOST,
                    help="bind address (use the docker gateway for Caddy)")
    ap.add_argument("--selftest", action="store_true",
                    help="exercise the cancel law on a throwaway sqlite db "
                         "and exit (no HTTP, no network)")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(_selftest())

    token = os.environ.get("QUEUE_TOKEN", "").strip()
    if not token:
        say("QUEUE_TOKEN is not set — refusing to start (fail closed)")
        sys.exit(1)

    if args.backend == "pg":
        try:
            backend = PGBackend()
        except ImportError:
            say("psycopg2 not installed — for local smoke use: --backend sqlite")
            sys.exit(1)
    else:
        backend = SQLiteBackend(args.db)

    try:
        backend.init()
    except Exception as exc:  # noqa: BLE001 — narrate, then die loudly
        say(f"backend init FAILED ({exc}) — for local smoke use: --backend sqlite")
        sys.exit(1)

    QueueHandler.backend = backend
    QueueHandler.token = token
    server = ThreadingHTTPServer((args.host, args.port), QueueHandler)
    say(f"listening on http://{HOST}:{args.port} token=***/ backend={backend.name}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("shutdown (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
