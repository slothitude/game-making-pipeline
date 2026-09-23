"""colab_worker — the GPU offload executor the queue router calls (PIPELINE.md law 9:
"GPU jobs -> the official Colab CLI").

  from cloud.colab.colab_worker import run_job
  result = run_job({
      "type": "gpu.train",            # gpu.train | gpu.mesh | gpu.render
      "staging_dir": "C:/.../stage",  # local inputs (data/, config.json, images, .blend)
      "return_dir":  "C:/.../done",   # out/ + result.json land here
      "gpu": "T4",                    # optional, T4 is the free/cheapest default
      "session": "gmp-train-1",       # optional runtime session name
      "payload": {...},               # optional, embedded verbatim into remote job.json
  })
  -> dict: the result.json the runtime script wrote (see cloud/colab/scripts/),
     or a structured error {"error": ..., "hint": "run colab auth", ...}.
     run_job NEVER raises for operational failures and ALWAYS tears the runtime
     down (try/finally) — free-tier discipline.

REAL CLI SURFACE (github.com/googlecolab/google-colab-cli README + docs/04,
researched 2026-09-23 — see cloud/colab/README.md for links and KNOWN_GAPS):
  colab new -s NAME --gpu T4        provision a runtime (T4/L4/G4/H100/A100/TPU)
  colab upload -s NAME LOCAL REMOTE push a file
  colab exec  -s NAME -f FILE       ship a LOCAL script and run it on the runtime
  colab download -s NAME REMOTE LOCAL
  colab stop -s NAME                terminate the VM + keep-alive daemon
  colab whoami / usage / version    auth + compute-unit debugging
Auth: --auth {oauth2,adc}, default adc; one-time human step is the gcloud ADC
login with the four Colab scopes (README "auth setup"). `colab auth` is NOT the
login — it auths the *VM* for GCS/BigQuery.

SELFTEST (no network, no real Colab — fakes the CLI with a stub shim):
  python cloud/colab/colab_worker.py --selftest
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
JOB_TIMEOUT_SEC = int(__import__("os").environ.get("GMP_JOB_TIMEOUT_SEC", "48")) * 60  # cold chains need the long window; env-tunable
POLL_SEC = 15                      # result.json download retry cadence
REMOTE_BASE = ""  # root-level names: subdir uploads 500 on fresh runtimes (probed 2026-09-23)
AUTH_MARKERS = ("401", "403", "unauthorized", "forbidden", "credential",
                "permission", "scope", "auth")

# crew/gate output carries non-cp1252 chars; a Windows console must not kill the lane
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log(msg):
    print(f"[colab_worker] {msg}", flush=True)


def _err(error, hint, **extra):
    out = {"error": error, "hint": hint}
    out.update(extra)
    return out


def _scripts_dir() -> Path:
    override = os.environ.get("GMP_COLAB_SCRIPTS")
    return Path(override) if override else HERE / "scripts"


# ---------------------------------------------------------------------------
# the CLI shim: every call goes through here so the 15-min deadline is enforced
# per-step and auth/CLI failures become structured errors, never hangs.
# ---------------------------------------------------------------------------
class Colab:
    def __init__(self, session, deadline):
        self.session = session
        self.deadline = deadline
        exe = shutil.which("colab")
        if not exe:
            raise _CliMissing()
        self.exe = exe

    def run(self, *args, timeout=None):
        budget = max(5.0, self.deadline - time.monotonic())
        if timeout:
            budget = min(budget, timeout)
        cmd = [self.exe, *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=budget,
                                  encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            raise _CliFailed("timeout", " ".join(cmd),
                             f"no output within {budget:.0f}s (job deadline)")
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    # --- real-command wrappers (see module docstring for the source) ---
    def new(self, gpu):
        rc, out = self.run("new", "-s", self.session, "--gpu", gpu)
        # fresh runtimes serve their content API before it's ready; give it a beat
        time.sleep(20)
        return rc, out

    def upload(self, local, remote):
        # cold content-API 500s: back off and retry before giving up
        last = ""
        for attempt in range(4):
            rc, out = self.run("upload", "-s", self.session,
                               str(Path(local).as_posix()), remote)
            if rc == 0 and "500" not in out and "Internal Server Error" not in out:
                return rc, out
            last = out
            log(f"upload attempt {attempt + 1} failed ({out[-120:]}); backing off")
            time.sleep(20 * (attempt + 1))
        return 1, last

    def exec_script(self, local_script):
        # Probed 2026-09-23: `exec -f FILE` breaks (kernel launcher rejects its
        # own -f) AND long stdin breaks the same way — only SHORT stdin execs
        # cleanly. So: upload the script as a root-level file (uploads work),
        # then exec the one-line bootstrap that runs it. Same deadline law.
        name = "gmp_runner.py"
        rc, out = self.upload(local_script, name)
        if rc != 0:
            return rc, out
        code = 'import os; os.chdir("/"); exec(open("/gmp_runner.py").read())'
        budget = max(5.0, self.deadline - time.monotonic())
        try:
            proc = subprocess.run(
                [self.exe, "exec", "-s", self.session], input=code,
                capture_output=True, timeout=budget,
                encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            raise _CliFailed("timeout", "exec bootstrap",
                             f"no completion within {budget:.0f}s (job deadline)")
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def download(self, remote, local):
        return self.run("download", "-s", self.session, remote, str(Path(local).as_posix()),
                        timeout=120)

    def stop(self):
        # teardown is best-effort but never skipped: even a dead CLI gets its shot
        try:
            return self.run("stop", "-s", self.session, timeout=90)
        except _CliFailed as exc:
            log(f"teardown call itself failed: {exc.detail}")
            return 1, str(exc.detail)


class _CliMissing(Exception):
    pass


class _CliFailed(Exception):
    def __init__(self, step, cmd, detail):
        super().__init__(f"{step} failed: {detail}")
        self.step = step
        self.cmd = cmd
        self.detail = detail


# ---------------------------------------------------------------------------
# run_job
# ---------------------------------------------------------------------------
def run_job(job: dict) -> dict:
    if not isinstance(job, dict):
        return _err("bad_job", "run_job(job: dict) — got " + type(job).__name__)
    jtype = job.get("type", "")
    runner_name = {"gpu.train": "train.py", "gpu.mesh": "mesh.py",
                   "gpu.render": "render.py", "gpu.anim": "anim.py",
                   "gpu.audio": "audio.py"}.get(jtype)
    if not runner_name:
        return _err("bad_job", f"type must be one of {sorted({'gpu.train', 'gpu.mesh', 'gpu.render', 'gpu.anim', 'gpu.audio'})}",
                    got=jtype)
    staging = Path(job.get("staging_dir", ""))
    return_dir = Path(job.get("return_dir", ""))
    if not staging.is_dir():
        return _err("bad_job", f"staging_dir does not exist: {staging}")
    if not return_dir.is_dir():
        try:
            return_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _err("bad_job", f"return_dir unusable: {exc}")

    try:
        Colab(None, time.monotonic())  # cheap presence probe before building anything
    except _CliMissing:
        return _err("cli_missing", "run colab auth",
                    detail="the 'colab' executable is not on PATH "
                           "(pip install google-colab-cli; Linux/macOS only — "
                           "Windows hosts need WSL, see README KNOWN_GAPS)")

    gpu = job.get("gpu", "T4")
    session = job.get("session") or f"gmp-{jtype.replace('.', '-')}-{int(time.time())}"
    started = time.monotonic()
    deadline = started + JOB_TIMEOUT_SEC
    runner = _scripts_dir() / runner_name
    if not runner.is_file():
        return _err("bad_job", f"runner script missing: {runner}")

    tmp = Path(tempfile.mkdtemp(prefix="gmp_colab_"))
    cli = Colab(session, deadline)
    log(f"job {jtype} session={session} gpu={gpu} staging={staging.as_posix()}")
    try:
        # 1. stage: staging contents + job.json -> in.zip (posix arcnames, Windows law)
        job_json = {"type": jtype, "payload": job.get("payload", {}),
                    "session": session, "gpu": gpu}
        inzip = tmp / "in.zip"
        n_staged = 0
        with zipfile.ZipFile(inzip, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(staging.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(staging).as_posix())
                    n_staged += 1
            z.writestr("job.json", json.dumps(job_json, indent=2))
            n_staged += 1
        log(f"staged {n_staged} files -> in.zip")

        # 2. provision
        rc, out = cli.new(gpu)
        if rc != 0:
            tail = out.strip()[-400:]
            low = out.lower()
            if any(m in low for m in AUTH_MARKERS):
                return _err("auth_failed", "run colab auth",
                            detail=tail, session=session,
                            note="real login is the gcloud ADC scopes command — README 'auth setup'")
            return _err("provision_failed", "check 'colab usage' (compute units) and "
                        "'colab whoami' (scopes)", detail=tail, session=session)
        log(f"runtime up ({gpu})")

        # 3. upload inputs, 4. execute the runner (exec -f ships the LOCAL script)
        rc, out = cli.upload(inzip, "in.zip")
        if rc != 0:
            return _err("upload_failed", "run colab auth", detail=out.strip()[-400:],
                        session=session)
        log("in.zip uploaded")

        rc, out = cli.exec_script(runner)
        if rc != 0:
            return _err("exec_failed", "inspect with 'colab log -s "
                        + session + "'", detail=out.strip()[-800:], session=session)
        log("runner executed")

        # 5. fetch results (exec's block-until-done behavior is a KNOWN_GAP: poll
        # for result.json so both sync and async exec semantics converge)
        result_local = tmp / "result.json"
        while True:
            rc, _ = cli.download("result.json", result_local)
            if rc == 0 and result_local.exists():
                break
            if time.monotonic() > deadline:
                return _err("timeout_or_no_result",
                            "runtime script never wrote result.json inside the 15-min cap",
                            session=session)
            log(f"no result.json yet — retry in {POLL_SEC}s")
            time.sleep(POLL_SEC)

        outzip = tmp / "out.zip"
        rc, out = cli.download("out.zip", outzip)
        if rc == 0 and outzip.exists():
            (return_dir / "out").mkdir(exist_ok=True)
            with zipfile.ZipFile(outzip) as z:
                z.extractall(return_dir / "out")
        else:
            log("no out.zip on the runtime (script wrote result.json only?)")

        shutil.copy(result_local, return_dir / "result.json")
        result = json.loads(result_local.read_text(encoding="utf-8"))
        log(f"job green in {time.monotonic() - started:.0f}s — artifacts in "
            f"{return_dir.as_posix()}")
        return result
    except _CliFailed as exc:
        return _err("cli_failed", "run colab auth", step=exc.step, cmd=exc.cmd,
                    detail=exc.detail, session=session)
    except Exception as exc:  # noqa: BLE001 — run_job is a queue boundary: report, never raise
        return _err("worker_error", "see detail", detail=repr(exc), session=session)
    finally:
        rc, out = cli.stop()
        log(f"teardown {'ok' if rc == 0 else 'ISSUE'}: session '{session}' stop rc={rc}"
            + ("" if rc == 0 else f" :: {out.strip()[-200:]}"))
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# --selftest: fakes the CLI with a stub shim, runs one gpu.train job end-to-end.
# No network, no real Colab, no torch (the shim runs a contract-faithful mini
# runner instead of scripts/train.py).
# ---------------------------------------------------------------------------
_SHIM = r'''
import json, os, shutil, subprocess, sys
from pathlib import Path
STATE = Path(os.environ["GMP_SHIM_STATE"])
REMOTE = STATE / "remote"
OPS = STATE / "ops.log"
def op(line):
    with open(OPS, "a", encoding="utf-8") as f: f.write(line + "\n")
def sess_live(name):
    return (STATE / (name + ".live")).exists()
def die(msg, code=1):
    sys.stderr.write(msg + "\n"); sys.exit(code)
a = sys.argv[1:]
name = a[a.index("-s") + 1] if "-s" in a else None
cmd = a[0] if a else ""
if cmd == "version":
    print("colab-cli 9.9.9-selftest-shim"); sys.exit(0)
if cmd == "usage":
    print("compute units: 42.0 remaining (shim)"); sys.exit(0)
if cmd == "whoami":
    print("selftest@gmp.shim, scopes: [colaboratory, userinfo.email]"); sys.exit(0)
if cmd == "new":
    if os.environ.get("GMP_SHIM_AUTHFAIL"):
        sys.stderr.write("HTTP 401 Unauthorized: request had no authentication scopes\n")
        sys.exit(1)
    if sess_live(name): die(f"session {name} already exists")
    (STATE / (name + ".live")).write_text("gpu=T4", encoding="utf-8")
    REMOTE.mkdir(exist_ok=True); op(f"new {name}"); sys.exit(0)
if cmd in ("upload", "download"):
    if not sess_live(name): die(f"session {name} is not live")
    if cmd == "upload":
        local, remote = Path(a[-2]), REMOTE / Path(a[-1]).name
        shutil.copy(local, remote); op(f"upload {local.name}")
    else:
        remote, local = REMOTE / Path(a[-2]).name, Path(a[-1])
        if not remote.exists(): die(f"not found: {remote.name}")  # exercises the poll loop
        shutil.copy(remote, local); op(f"download {remote.name}")
    sys.exit(0)
if cmd == "exec":
    if not sess_live(name): die(f"session {name} is not live")
    script = Path(a[a.index("-f") + 1])
    op(f"exec {script.name}")
    env = dict(os.environ, GMP_COLAB_BASE=str(REMOTE))
    proc = subprocess.run([sys.executable, str(script)], env=env,
                          cwd=REMOTE, capture_output=True, text=True)
    sys.stdout.write(proc.stdout); sys.stderr.write(proc.stderr)
    sys.exit(proc.returncode)
if cmd == "stop":
    if not sess_live(name): die(f"session {name} was never live")
    (STATE / (name + ".live")).unlink(); op(f"stop {name}"); sys.exit(0)
die(f"shim: unknown command {' '.join(a)}")
'''

# the shim-executed stand-in for scripts/train.py: same contract, stdlib-only
_MINI_TRAIN = r'''
import json, time, zipfile
from pathlib import Path
import os
base = None
for cand in (Path(os.environ.get("GMP_COLAB_BASE", "/content/gmp_job")), Path.cwd()):
    if (cand / "in.zip").exists():
        base = cand
        break
with zipfile.ZipFile(base / "in.zip") as z:
    names = z.namelist()
    z.extractall(base / "work")
job = json.loads((base / "work" / "job.json").read_text(encoding="utf-8"))
out = base / "out"
out.mkdir(parents=True, exist_ok=True)
(out / "model.pt").write_bytes(b"gmp-selftest-weights:" + json.dumps(job["payload"]).encode())
result = {"ok": True, "type": "gpu.train", "stub": "selftest mini runner",
          "staged_files": names, "payload_seen": job["payload"],
          "device": "shim-cpu", "seconds": 0.1, "final_loss": 0.0}
(base / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
with zipfile.ZipFile(base / "out.zip", "w") as z:
    z.write(out / "model.pt", "model.pt")
print("[mini-train] job.json read, model.pt + result.json + out.zip written")
'''


def _write_shim(bin_dir: Path):
    shim_py = bin_dir / "colab_shim.py"
    shim_py.write_text(_SHIM, encoding="utf-8")
    if os.name == "nt":
        bat = bin_dir / "colab.bat"
        bat.write_text('@python "%~dp0colab_shim.py" %*\n', encoding="ascii")
        return bat
    sh = bin_dir / "colab"
    sh.write_text("#!/bin/sh\nexec python3 \"$(dirname \"$0\")/colab_shim.py\" \"$@\"\n",
                  encoding="ascii")
    sh.chmod(0o755)
    return sh


def _selftest() -> int:
    log("SELFTEST start — fake CLI, no network, no real Colab")
    tmp = Path(tempfile.mkdtemp(prefix="gmp_selftest_"))
    bin_dir, state = tmp / "bin", tmp / "state"
    staging, ret = tmp / "stage", tmp / "done"
    scripts = tmp / "scripts"
    for d in (bin_dir, state, staging / "data", ret, scripts):
        d.mkdir(parents=True)
    _write_shim(bin_dir)
    (scripts / "train.py").write_text(_MINI_TRAIN, encoding="utf-8")
    (staging / "config.json").write_text(json.dumps({"epochs": 2, "d_model": 32}),
                                         encoding="utf-8")
    (staging / "data" / "train.jsonl").write_text(
        "\n".join(json.dumps({"text": f"jevlike sample {i}"}) for i in range(4)),
        encoding="utf-8")
    os.environ["GMP_SHIM_STATE"] = str(state)
    os.environ["GMP_COLAB_SCRIPTS"] = str(scripts)
    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    if os.name == "nt":
        os.environ["PATHEXT"] = os.environ.get("PATHEXT", "") + ";.BAT"

    job = {"type": "gpu.train", "staging_dir": str(staging), "return_dir": str(ret),
           "gpu": "T4", "session": "selftest-1",
           "payload": {"game": "sonar", "epochs": 2}}
    log(f"run_job({json.dumps(job)})")
    result = run_job(job)
    log("result.json contents -> " + json.dumps(result))

    ops = (state / "ops.log").read_text(encoding="utf-8").splitlines()
    log("shim oplog: " + " -> ".join(o.split(None, 1)[0] for o in ops))
    checks = [
        ("result ok", result.get("ok") is True and result.get("type") == "gpu.train"),
        ("payload survived the round-trip", result.get("payload_seen", {}).get("game") == "sonar"),
        ("model.pt downloaded", (ret / "out" / "model.pt").is_file()),
        ("result.json downloaded", (ret / "result.json").is_file()),
        ("lifecycle new->upload->exec->download(result)->download(out)->stop",
            [o.split(None, 1)[0] for o in ops]
            == ["new", "upload", "exec", "download", "download", "stop"]),
        ("runtime torn down", not (state / "selftest-1.live").exists()),
        ("both artifacts downloaded (result.json + out.zip)",
            (state / "ops.log").read_text(encoding="utf-8").count("download") == 2),
    ]
    ok = True
    for name, passed in checks:
        log(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    # scenario 2: auth failure -> structured error with the spec'd hint
    os.environ["GMP_SHIM_AUTHFAIL"] = "1"
    log("scenario 2: auth failure path")
    bad = run_job({"type": "gpu.train", "staging_dir": str(staging),
                   "return_dir": str(ret), "session": "selftest-auth"})
    log("auth-failure result -> " + json.dumps(bad))
    auth_ok = bad.get("error") == "auth_failed" and bad.get("hint") == "run colab auth"
    log(f"  [{'PASS' if auth_ok else 'FAIL'}] structured auth error, hint 'run colab auth'")
    ok = ok and auth_ok

    log(f"SELFTEST {'GREEN' if ok else 'RED'} (tmp {tmp})")
    if ok:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="GMP Colab GPU offload worker")
    ap.add_argument("--selftest", action="store_true",
                    help="fake-CLI end-to-end test of one gpu.train job")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(_selftest())
    ap.print_help()


if __name__ == "__main__":
    main()
