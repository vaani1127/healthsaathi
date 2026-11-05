"""Recording overhead load test on the prod-demo VM (SPEC 8, RQ5), against a separate copy.

    python -m hs_demo.loadtest bench-db     # make load-bench-db: create and seed healthsaathi_bench
    python -m hs_demo.loadtest run          # make load-run: both variants, results copied back
    python -m hs_demo.loadtest run --reversed   # make load-run-reversed: no-recording first
    python -m hs_demo.loadtest clean        # make load-clean: remove the copy and its database

The copy is a second API container on the VM (`hs-bench`, port 8100 on localhost only) with
APP_ENV=test and its own database `healthsaathi_bench` on the same server, seeded with the demo
clinics. It runs twice: with recording, then with BENCHMARK_SKIP_RECORDING=true, which the API
refuses outside APP_ENV=test. `--reversed` runs the same two variants in the other order and saves
them with a `_reversed` suffix, to show how much of the difference comes from run order. The live
API and database are never switched. Locust runs on the VM too, so the network is not in the
numbers; both runs share the VM with the live demo.

Reaching the database from this computer goes through an SSH tunnel on localhost:15432 (Cloudflare
WARP blocks port 5432): `ssh -L 15432:<db host>:5432 azureuser@<vm>`. Results go to
experiments/results/load/<variant>[_reversed]_*.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.dburl import asyncpg_url, engine_options
from hs_demo import hosted
from hs_demo.native import ROOT, tool

BENCH_DB = "healthsaathi_bench"
BENCH_STATE = ROOT / ".demo" / "hosted-bench"
RESULTS = ROOT / "experiments" / "results" / "load"
REMOTE = "/opt/healthsaathi/load"
LOCUST_IMAGE = "locustio/locust:2.43.1"
USERS = 10
SPAWN_RATE = 2
DURATION = "240s"
VARIANTS = {"recording": "false", "no-recording": "true"}
TUNNEL = "localhost:15432"


def say(message: str) -> None:
    print(f"[load] {message}", flush=True)


def db_host(values: dict[str, str]) -> str:
    return f"healthsaathi-{values['AZURE_SUFFIX']}.postgres.database.azure.com:5432"


def bench_url(url: str, host: str, *, via: str | None = None) -> str:
    """The same login on the benchmark database, optionally through the tunnel."""
    out = url.replace(f"{host}/healthsaathi?", f"{host}/{BENCH_DB}?")
    return out.replace(host, via) if via else out


def bench_values(values: dict[str, str], via: str | None) -> dict[str, str]:
    host = db_host(values)
    keys = ("DATABASE_URL", "MIGRATOR_DATABASE_URL", "BACKUP_DATABASE_URL")
    return {**values, **{k: bench_url(values[k], host, via=via) for k in keys}}


async def create_database(admin_url: str) -> None:
    url = asyncpg_url(admin_url)
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT", **engine_options(url))
    try:
        async with engine.connect() as conn:
            found = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": BENCH_DB}
            )
            if not found:
                await conn.execute(text(f'CREATE DATABASE "{BENCH_DB}"'))
                say(f"created database {BENCH_DB}")
    finally:
        await engine.dispose()


def bench_db() -> None:
    values = hosted.read_values()
    via = bench_values(values, TUNNEL)
    asyncio.run(create_database(values["MIGRATOR_DATABASE_URL"].replace(db_host(values), TUNNEL)))
    BENCH_STATE.mkdir(parents=True, exist_ok=True)
    env_file = BENCH_STATE / "tunnel.env"
    hosted.write_values(env_file, via)
    env = {**os.environ, "HOSTED_ENV": str(env_file), "HOSTED_STATE": str(BENCH_STATE)}
    try:
        for step in ("db", "seed"):
            hosted.run([sys.executable, "-m", "hs_demo.hosted", step], ROOT, env)
    finally:
        env_file.unlink()
    say("benchmark database ready")


def ssh(target: str, command: str) -> str:
    done = subprocess.run(  # noqa: S603
        [tool("ssh"), *hosted.ssh_options(), target, command],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def remote_script(variant: str, skip: str, tag: str) -> str:
    """One variant on the VM: start the copy, wait until ready, load it while sampling memory."""
    return f"""set -eu
cd {REMOTE}
sudo docker rm -f hs-bench >/dev/null 2>&1 || true
trap 'sudo docker rm -f hs-bench >/dev/null 2>&1 || true' EXIT
sudo docker run -d --name hs-bench --env-file /opt/healthsaathi/.env \\
  --env-file {REMOTE}/bench.env -e BENCHMARK_SKIP_RECORDING={skip} \\
  -p 127.0.0.1:8100:8000 healthsaathi-api:{tag} >/dev/null
for _ in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8100/api/v1/ready >/dev/null 2>&1 && break
  sleep 2
done
mkdir -p out
( while sleep 5; do sudo docker stats --no-stream --format '{{{{.MemUsage}}}}' hs-bench; done ) \\
  > out/{variant}_memory.txt 2>/dev/null &
SAMPLER=$!
sudo docker run --rm --network host -v {REMOTE}:/load --entrypoint sh {LOCUST_IMAGE} -c \\
  "pip install -q pyotp >/dev/null 2>&1 && locust -f /load/locustfile.py --headless -u {USERS} \\
  -r {SPAWN_RATE} -t {DURATION} --host http://127.0.0.1:8100 --csv /load/out/{variant} \\
  --only-summary --exit-code-on-error 0" > out/{variant}_locust.log 2>&1
kill $SAMPLER
echo done {variant}
"""


def load_config() -> dict[str, object]:
    data = json.loads((BENCH_STATE / "logins.json").read_text(encoding="utf-8"))
    return hosted.activity_config(data)


def bench_env(values: dict[str, str]) -> dict[str, str]:
    """Overrides for the copy: its database, test mode, no rate limit, no anchoring."""
    bench = bench_values(values, None)
    return {
        "APP_ENV": "test",
        "DATABASE_URL": bench["DATABASE_URL"],
        "MIGRATOR_DATABASE_URL": bench["MIGRATOR_DATABASE_URL"],
        "RATE_LIMIT_ENABLED": "false",
        "ANCHOR_FALLBACK_MINUTES": "0",
        "EMAIL_BACKEND": "console",
    }


def run_order(reverse: bool) -> tuple[str, list[tuple[str, str]]]:
    """The file suffix and the (variant, switch) pairs in the order they run."""
    pairs = list(VARIANTS.items())
    return ("_reversed", pairs[::-1]) if reverse else ("", pairs)


def run(reverse: bool = False) -> None:
    suffix, order = run_order(reverse)
    values = hosted.read_values()
    target = f"{values['VM_USER']}@{values['VM_HOST']}"
    # A fresh out/ so only this run's files are copied back.
    ssh(target, f"mkdir -p {REMOTE} && chmod 700 {REMOTE} && sudo rm -rf {REMOTE}/out")
    staging = BENCH_STATE / "upload"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "logins.json").write_bytes(json.dumps(load_config()).encode())
    hosted.write_values(staging / "bench.env", bench_env(values))
    env = dict(os.environ)
    try:
        files = [str(ROOT / "infra" / "load" / "locustfile.py"), *map(str, staging.iterdir())]
        hosted.run([tool("scp"), *hosted.ssh_options(), *files, f"{target}:{REMOTE}/"], ROOT, env)
    finally:
        for f in staging.iterdir():
            f.unlink()
    tag = ssh(target, "cat /opt/healthsaathi/current_tag").strip()
    # An image from before the switch would ignore it and record in the "no-recording" run.
    # Importing the service loads the settings, so the probe needs the server's env file.
    probe = (
        "sudo docker run --rm --env-file /opt/healthsaathi/.env --entrypoint python "
        f"healthsaathi-api:{tag} -c "
        "\"from app.access.service import skip_recording; print('switch present')\""
    )
    if "switch present" not in ssh(target, f"{probe} 2>/dev/null || true"):
        raise SystemExit(f"healthsaathi-api:{tag} has no BENCHMARK_SKIP_RECORDING; deploy first")
    for variant, skip in order:
        say(f"{variant}: {USERS} users for {DURATION} against the copy of {tag}")
        say(ssh(target, remote_script(f"{variant}{suffix}", skip, tag)).strip())
    RESULTS.mkdir(parents=True, exist_ok=True)
    hosted.run(
        [tool("scp"), *hosted.ssh_options(), "-r", f"{target}:{REMOTE}/out/.", str(RESULTS)],
        ROOT,
        env,
    )
    (RESULTS / f"setup{suffix}.txt").write_text(
        f"api_image=healthsaathi-api:{tag}\nlocust={LOCUST_IMAGE}\nusers={USERS}\n"
        f"order={','.join(v for v, _ in order)}\n"
        f"spawn_rate={SPAWN_RATE}\nduration={DURATION}\nvm=Azure {values.get('AZURE_LOCATION')}"
        " Standard_B2ats_v2 (2 vCPU, 1 GB), shared with the live demo\n",
        encoding="utf-8",
    )
    say(f"results in {RESULTS}")


async def drop_database(admin_url: str) -> None:
    url = asyncpg_url(admin_url)
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT", **engine_options(url))
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{BENCH_DB}" WITH (FORCE)'))
    finally:
        await engine.dispose()


def clean() -> None:
    """Remove the benchmark copy: its container, its files on the VM, its database, local state."""
    values = hosted.read_values()
    target = f"{values['VM_USER']}@{values['VM_HOST']}"
    ssh(target, f"sudo docker rm -f hs-bench >/dev/null 2>&1 || true; sudo rm -rf {REMOTE}")
    say(f"removed hs-bench and {REMOTE} on the VM")
    asyncio.run(drop_database(values["MIGRATOR_DATABASE_URL"].replace(db_host(values), TUNNEL)))
    say(f"dropped database {BENCH_DB}")
    if BENCH_STATE.exists():
        shutil.rmtree(BENCH_STATE)
        say(f"removed {BENCH_STATE}")


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    if command == "bench-db":
        bench_db()
    elif command == "run":
        run(reverse="--reversed" in argv[1:])
    elif command == "clean":
        clean()
    else:
        raise SystemExit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
