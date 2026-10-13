"""Native demo: the whole app on this machine without Docker, with a hosted Postgres (Neon).

    python -m hs_demo.native up          # make demo-native
    python -m hs_demo.native stop        # make demo-native-stop
    python -m hs_demo.native reset       # make demo-reset-native (wipe the database and state)
    python -m hs_demo.native run MODULE [ARGS]   # run a module with the demo environment
    python -m hs_demo.native code EMAIL  # current authenticator code of a demo login
    python -m hs_demo.native restart-api # restart the api (DEMO_RATE_LIMIT=false for tests)
    python -m hs_demo.native sql "..."   # one statement as the database owner (tamper demo)

Database URLs come from .env.local (see .env.local.example): CONNECT_CONNECTION_POOLING_ON
(pooled) for the api, CONNECT_CONNECTION_POOLING_OFF (direct) for migrations, the seed and the
worker (the worker holds a session-level
advisory lock and LISTEN needs a session, which a transaction pooler does not keep). Secrets,
logins, emails and models live in .demo/state; witness commits in .demo/witness; process logs in
.demo/logs. Nothing here is committed.
"""

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
DEMO = ROOT / ".demo"
STATE = DEMO / "state"
LOGS = DEMO / "logs"
PIDS = DEMO / "pids"
WITNESS = DEMO / "witness"
ANVIL_STATE = DEMO / "anvil" / "state.json"
API_DIR = ROOT / "apps" / "api"
WEB_DIR = ROOT / "apps" / "web"
CONTRACTS = ROOT / "contracts"
RPC = "http://127.0.0.1:8545"
API_PORT = 8081
WEB_PORT = 8080
WINDOWS = os.name == "nt"


def say(message: str) -> None:
    print(f"[demo-native] {message}", flush=True)


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


# Names used in .env.local (see .env.local.example).
POOLED = "CONNECT_CONNECTION_POOLING_ON"
DIRECT = "CONNECT_CONNECTION_POOLING_OFF"
TEST_POOLED = "TEST_BRANCH_CONNECTION_POOLING_ON"
TEST_DIRECT = "TEST_BRANCH_CONNECTION_POOLING_OFF"


def local_env() -> dict[str, str]:
    values = read_env_file(ROOT / ".env.local")
    missing = [k for k in (POOLED, DIRECT) if not values.get(k)]
    if missing:
        raise SystemExit(f".env.local is missing {', '.join(missing)} (see .env.local.example)")
    return values


def demo_env(*, direct: bool = False) -> dict[str, str]:
    """The environment of every demo process. Values are never printed."""
    db = local_env()
    env = dict(os.environ)
    env.update(read_env_file(STATE / "demo.env"))
    env.update(
        {
            "APP_ENV": "local",
            "DATABASE_URL": db[DIRECT] if direct else db[POOLED],
            "DATABASE_DIRECT_URL": db[DIRECT],
            # Neon's pooler keeps prepared statements, so their caches stay on.
            "DATABASE_POOLER_PREPARED_STATEMENTS": "true",
            "MIGRATOR_DATABASE_URL": db[DIRECT],
            "DEMO_STATE_DIR": str(STATE),
            "SIM_PROFILES_DIR": str(ROOT / "sim" / "profiles"),
            "CORS_ORIGINS": f'["http://localhost:{WEB_PORT}"]',
            "PUBLIC_APP_URL": f"http://localhost:{WEB_PORT}",
            "COOKIE_SECURE": "true",
            "RATE_LIMIT_ENABLED": os.environ.get("DEMO_RATE_LIMIT", "true"),
            "EMAIL_BACKEND": "console",
            "EMAIL_OUTBOX_DIR": str(STATE / "outbox"),
            "STORAGE_DIR": str(STATE / "blobs"),
            "MODEL_DIR": str(STATE / "models"),
            "ANCHOR_RPC_URL": RPC,
            "ANCHOR_CHAIN": "anvil",
            "ANCHOR_FALLBACK_MINUTES": "2",
            "WITNESS_DIR": str(WITNESS),
            "OTS_ENABLED": "false",
            "DETECT_INTERVAL_SECONDS": "60",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


# Processes ----------------------------------------------------------------------------------------


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start(name: str, args: list[str], cwd: Path, env: dict[str, str]) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    PIDS.mkdir(parents=True, exist_ok=True)
    log = (LOGS / f"{name}.log").open("ab")
    flags = 0
    if WINDOWS:
        # Survives the make process; stopped with taskkill on its process tree.
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    proc = subprocess.Popen(  # noqa: S603 - fixed commands
        args,
        cwd=cwd,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=flags,
        start_new_session=not WINDOWS,
    )
    (PIDS / f"{name}.pid").write_text(str(proc.pid), encoding="utf-8")
    say(f"started {name} (pid {proc.pid}, log .demo/logs/{name}.log)")


def kill(name: str) -> None:
    pid_file = PIDS / f"{name}.pid"
    if not pid_file.exists():
        return
    pid = int(pid_file.read_text(encoding="utf-8"))
    if WINDOWS:
        subprocess.run(  # noqa: S603
            ["taskkill", "/PID", str(pid), "/T", "/F"],  # noqa: S607
            capture_output=True,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            getattr(os, "killpg")(pid, signal.SIGTERM)  # noqa: B009 - POSIX only
    pid_file.unlink()
    say(f"stopped {name}")


def run(args: list[str], cwd: Path, env: dict[str, str]) -> None:
    result = subprocess.run(args, cwd=cwd, env=env, check=False)  # noqa: S603
    if result.returncode != 0:
        raise SystemExit(f"failed: {' '.join(args[:4])} (exit {result.returncode})")


def wait_for(check: object, what: str, seconds: int = 120) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():  # type: ignore[operator]
            return
        time.sleep(1)
    raise SystemExit(f"{what} did not come up in {seconds} s; see .demo/logs")


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - local URL
            return bool(resp.status == 200)
    except Exception:
        return False


def rpc(method: str, params: list[object]) -> object:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - local anvil
        return json.loads(resp.read())["result"]


def tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"{name} is not installed or not on PATH")
    return found


# Steps --------------------------------------------------------------------------------------------


def start_anvil() -> None:
    if port_open(8545):
        say("anvil already running")
        return
    ANVIL_STATE.parent.mkdir(parents=True, exist_ok=True)
    args = [tool("anvil"), "--host", "127.0.0.1", "--chain-id", "31337", "--silent"]
    args += ["--state", str(ANVIL_STATE), "--state-interval", "10"]
    start("anvil", args, ROOT, dict(os.environ))
    wait_for(lambda: port_open(8545), "anvil", 30)


def deploy_contracts() -> None:
    target = STATE / "anvil.json"
    if target.exists():
        address = json.loads(target.read_text(encoding="utf-8"))["AuditAnchor"]
        if rpc("eth_getCode", [address, "latest"]) not in ("0x", None):
            say("contracts already deployed")
            return
    admins = rpc("eth_accounts", [])
    assert isinstance(admins, list) and len(admins) >= 3
    env = dict(os.environ)
    env.update({"DEPLOY_NETWORK": "anvil"})
    env.update({f"REGISTRY_ADMIN_{i + 1}": str(admins[i]) for i in range(3)})
    args = [tool("forge"), "script", "script/Deploy.s.sol", "--rpc-url", RPC, "--broadcast"]
    args += ["--unlocked", "--sender", str(admins[0]), "--silent"]
    run(args, CONTRACTS, env)
    STATE.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CONTRACTS / "deployments" / "anvil.json", target)
    say("contracts deployed to anvil")


def build_web() -> None:
    dist = WEB_DIR / "dist"
    src_times = [p.stat().st_mtime for p in (WEB_DIR / "src").rglob("*") if p.is_file()]
    if (dist / "index.html").exists() and (dist / "index.html").stat().st_mtime > max(src_times):
        say("web build is up to date")
        return
    env = dict(os.environ)
    env["VITE_API_URL"] = ""  # same origin: the web server proxies /api
    run([tool("pnpm"), "--filter", "@healthsaathi/web", "build"], ROOT, env)


def start_api() -> None:
    kill("api")
    start(
        "api",
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(API_PORT),
        ],
        API_DIR,
        demo_env(),
    )
    wait_for(lambda: http_ok(f"http://127.0.0.1:{API_PORT}/api/v1/ready"), "api", 300)


def up() -> None:
    local_env()  # fail early without .env.local
    for name in ("api", "worker", "web"):
        kill(name)
    start_anvil()
    deploy_contracts()
    env = dict(os.environ, DEMO_STATE_DIR=str(STATE))
    run([sys.executable, "-m", "hs_demo.init"], ROOT, env)
    say("migrating the database (direct connection)")
    run([sys.executable, "-m", "alembic", "upgrade", "head"], API_DIR, demo_env(direct=True))
    say("seeding (first run only: simulation, loading, detection, anchoring)")
    run([sys.executable, "-m", "hs_demo.seed"], API_DIR, demo_env(direct=True))
    build_web()
    python = sys.executable
    start_api()
    start("worker", [python, "-m", "app.worker"], API_DIR, demo_env(direct=True))
    start(
        "web",
        [
            tool("node"),
            str(ROOT / "infra" / "demo" / "serve-web.mjs"),
            str(WEB_DIR / "dist"),
            str(STATE),
            str(WEB_PORT),
            str(API_PORT),
        ],
        ROOT,
        dict(os.environ),
    )
    wait_for(lambda: http_ok(f"http://127.0.0.1:{WEB_PORT}/"), "web", 60)
    run([python, "-m", "hs_demo.logins"], ROOT, env)


def stop() -> None:
    for name in ("web", "worker", "api", "anvil"):
        kill(name)


def reset() -> None:
    """Stop everything, empty the demo database and remove the local demo state."""
    stop()
    say("emptying the database (public and research schemas)")
    run([sys.executable, "-m", "hs_demo.native", "wipe-db"], ROOT, demo_env(direct=True))
    for path in (STATE, WITNESS, ANVIL_STATE.parent):
        shutil.rmtree(path, ignore_errors=True)
    say("demo state removed; run make demo-native to start fresh")


def sql(statement: str) -> None:
    """Run one SQL statement as the database owner and print the first value (tamper demo)."""
    import asyncio

    import asyncpg

    from app.core.dburl import asyncpg_dsn

    async def main() -> None:
        conn = await asyncpg.connect(asyncpg_dsn(os.environ["DATABASE_DIRECT_URL"]))
        try:
            if ";" in statement.strip().rstrip(";"):
                await conn.execute(statement)  # several statements, for example BEGIN ... COMMIT
            else:
                print(await conn.fetchval(statement) or "")
        finally:
            await conn.close()

    asyncio.run(main())


def wipe_db() -> None:
    import asyncio

    import asyncpg

    from app.core.dburl import asyncpg_dsn

    async def main() -> None:
        conn = await asyncpg.connect(asyncpg_dsn(os.environ["DATABASE_DIRECT_URL"]))
        try:
            await conn.execute("DROP SCHEMA IF EXISTS research CASCADE")
            await conn.execute("DROP SCHEMA public CASCADE")
            await conn.execute("CREATE SCHEMA public")
        finally:
            await conn.close()

    asyncio.run(main())


SMOKE_TESTS = [
    "tests/test_health.py",
    "tests/test_dburl.py",
    "tests/db/test_roles.py",
    "tests/db/test_rls.py",
    "tests/identity/test_auth.py",
    "tests/anchor/test_receipts.py",
]


def smoke(extra: list[str]) -> int:
    """API and database smoke tests against the Neon branch "test" (the full suite runs in CI).
    The test setup creates and migrates its own database there, healthsaathi_test."""
    values = read_env_file(ROOT / ".env.local")
    if not values.get(TEST_DIRECT):
        raise SystemExit(f".env.local is missing {TEST_DIRECT}")
    env = dict(os.environ)
    env["DATABASE_URL"] = values[TEST_DIRECT]
    env.pop("MIGRATOR_DATABASE_URL", None)
    args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *(extra or SMOKE_TESTS)]
    return subprocess.run(args, cwd=API_DIR, env=env, check=False).returncode  # noqa: S603


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "up"
    if command == "up":
        up()
    elif command == "stop":
        stop()
    elif command == "reset":
        reset()
    elif command == "wipe-db":
        wipe_db()
    elif command == "smoke":
        return smoke(argv[1:])
    elif command == "restart-api":
        start_api()
    elif command == "sql":
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "hs_demo.native", "_sql", argv[1]],
            cwd=ROOT,
            env=demo_env(direct=True),
            check=False,
        )
        return result.returncode
    elif command == "_sql":
        sql(argv[1])
    elif command == "run":
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", *argv[1:]], cwd=API_DIR, env=demo_env(direct=True), check=False
        )
        return result.returncode
    elif command == "code":
        os.environ["DEMO_STATE_DIR"] = str(STATE)
        from hs_demo.logins import current_code

        print(current_code(argv[1]))
    else:
        raise SystemExit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
