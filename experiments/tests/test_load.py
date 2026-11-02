from pathlib import Path

from e2d_experiments import load

HEADER = (
    "Type,Name,Request Count,Failure Count,Median Response Time,Average Response Time,"
    "Min Response Time,Max Response Time,Average Content Size,Requests/s,Failures/s,"
    "50%,66%,75%,80%,90%,95%,98%,99%,99.9%,99.99%,100%"
)


def stats_csv(p50: int, p95: int) -> str:
    rows = [
        f"GET,nurse chart,100,0,{p50},1,1,1,1,4.0,0,{p50},1,1,1,1,{p95},1,1,1,1,1",
        "GET,auth me (control),40,1,10,1,1,1,1,1.5,0,10,1,1,1,1,20,1,1,1,1,1",
        f",Aggregated,140,1,{p50},1,1,1,1,5.5,0,{p50},1,1,1,1,{p95},1,1,1,1,1",
    ]
    return HEADER + "\n" + "\n".join(rows) + "\n"


def test_memory_lines_are_read_in_mib() -> None:
    text = "245.3MiB / 887.1MiB\n1.5GiB / 2GiB\nnoise\n512KiB / 1GiB\n"
    assert load.memory_mib(text) == [245.3, 1536.0, 0.5]


def test_table_compares_both_variants_per_endpoint(tmp_path: Path) -> None:
    (tmp_path / "recording_stats.csv").write_text(stats_csv(80, 200), encoding="utf-8")
    (tmp_path / "no-recording_stats.csv").write_text(stats_csv(40, 90), encoding="utf-8")
    (tmp_path / "recording_memory.txt").write_text(
        "300MiB / 1GiB\n310MiB / 1GiB\n", encoding="utf-8"
    )
    (tmp_path / "setup.txt").write_text(
        "users=10\nduration=240s\nvm=Azure test VM\n", encoding="utf-8"
    )
    out = load.write(tmp_path, tmp_path / "tables")
    text = out.read_text(encoding="utf-8")
    assert "nurse chart & With recording & 100 & 0 & 80 & 200" in text
    assert "nurse chart & Without recording & 100 & 0 & 40 & 90" in text
    assert "Aggregated" not in text
    assert "API memory (MiB), with recording &  &  &  & 310 & 310" in text
    assert "10 simulated users for 240s on Azure test VM" in text
