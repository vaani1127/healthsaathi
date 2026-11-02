from hs_demo import hosted, loadtest

KEY = "0x" + "1" * 64


def env() -> dict[str, str]:
    return hosted.build_env("example.org", "a@example.org", "a@gmail.com", KEY)


def test_the_copy_never_points_at_the_live_database() -> None:
    values = env()
    bench = loadtest.bench_env(values)
    assert bench["APP_ENV"] == "test"
    for key in ("DATABASE_URL", "MIGRATOR_DATABASE_URL"):
        assert f"/{loadtest.BENCH_DB}?" in bench[key]
        assert "/healthsaathi?" not in bench[key]
        assert bench[key] != values[key]
    # The same logins: hs_app for the app, the admin for migrations.
    assert "hs_app:" in bench["DATABASE_URL"] and "hsadmin:" in bench["MIGRATOR_DATABASE_URL"]
    assert bench["RATE_LIMIT_ENABLED"] == "false" and bench["ANCHOR_FALLBACK_MINUTES"] == "0"


def test_tunnel_urls_keep_the_database_name_and_use_localhost() -> None:
    values = env()
    via = loadtest.bench_values(values, loadtest.TUNNEL)
    assert via["DATABASE_URL"].count(loadtest.TUNNEL) == 1
    assert f"{loadtest.TUNNEL}/{loadtest.BENCH_DB}?" in via["MIGRATOR_DATABASE_URL"]
    assert "database.azure.com" not in via["DATABASE_URL"]


def test_each_variant_sets_the_switch_and_only_listens_on_localhost() -> None:
    off = loadtest.remote_script("recording", "false", "v9.9.9")
    on = loadtest.remote_script("no-recording", "true", "v9.9.9")
    assert "BENCHMARK_SKIP_RECORDING=false" in off and "BENCHMARK_SKIP_RECORDING=true" in on
    for script in (off, on):
        assert "-p 127.0.0.1:8100:8000" in script
        assert "healthsaathi-api:v9.9.9" in script
        assert "--env-file /opt/healthsaathi/.env" in script and "bench.env" in script
        assert "docker rm -f hs-bench" in script
    assert loadtest.VARIANTS == {"recording": "false", "no-recording": "true"}
