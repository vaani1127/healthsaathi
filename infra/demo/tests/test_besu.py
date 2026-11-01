import yaml

from hs_demo import besu


def test_genesis_enables_what_the_contracts_need() -> None:
    accounts = ["0x" + "11" * 20, "0x" + "22" * 20]
    config = besu.genesis_config(accounts)
    chain = config["genesis"]["config"]
    # contracts/foundry.toml compiles for cancun.
    assert chain["cancunTime"] == 0 and chain["shanghaiTime"] == 0
    assert chain["qbft"]["blockperiodseconds"] == besu.BLOCK_PERIOD_SECONDS
    assert config["blockchain"]["nodes"] == {"generate": True, "count": 4}
    assert set(config["genesis"]["alloc"]) == {"11" * 20, "22" * 20}


def test_compose_has_four_validators_and_one_open_rpc_port() -> None:
    compose = yaml.safe_load(besu.compose_file("ab" * 64))
    nodes = compose["services"]
    assert sorted(nodes) == ["node1", "node2", "node3", "node4"]
    assert [n for n, s in nodes.items() if "ports" in s] == ["node1"]
    assert nodes["node1"]["ports"] == ["8545:8545"]
    boot = f"--bootnodes=enode://{'ab' * 64}@{besu.SUBNET}.11:30303"
    assert boot not in nodes["node1"]["command"]
    assert all(boot in nodes[f"node{i}"]["command"] for i in (2, 3, 4))
    assert all(s["image"] == besu.IMAGE for s in nodes.values())
