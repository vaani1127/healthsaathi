"""The compose files let only Caddy set the client IP (X-Forwarded-For)."""

import ipaddress
from pathlib import Path
from typing import Any

import pytest
import yaml

INFRA = Path(__file__).resolve().parents[2]


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((INFRA / name).read_text(encoding="utf-8"))
    return data


@pytest.mark.parametrize(
    ("compose", "caddy", "api_env"),
    [
        ("docker-compose.prod.yml", "caddy", lambda c: c["x-api"]["environment"]),
        ("docker-compose.demo.yml", "web", lambda c: c["x-demo"]["environment"]),
    ],
)
def test_only_caddy_is_trusted(compose: str, caddy: str, api_env: Any) -> None:
    config = load(compose)
    trusted = api_env(config)["TRUSTED_PROXY_IPS"]
    assert "*" not in trusted
    address = config["services"][caddy]["networks"]["default"]["ipv4_address"]
    assert trusted == address
    ipam = config["networks"]["default"]["ipam"]["config"][0]
    subnet = ipaddress.ip_network(ipam["subnet"])
    dynamic = ipaddress.ip_network(ipam["ip_range"])
    # Caddy's address is in the subnet but outside the range other containers are given.
    assert ipaddress.ip_address(address) in subnet
    assert ipaddress.ip_address(address) not in dynamic
    # Kept for rolling back to an image that still reads it; only ever Caddy's address.
    assert api_env(config)["FORWARDED_ALLOW_IPS"] == address


def test_uvicorn_does_not_read_proxy_headers_itself() -> None:
    dockerfile = (INFRA / "api.Dockerfile").read_text(encoding="utf-8")
    assert "--no-proxy-headers" in dockerfile
    assert '"--proxy-headers"' not in dockerfile
    demo_api = load("docker-compose.demo.yml")["services"]["api"]["command"]
    assert "--no-proxy-headers" in demo_api


def test_prod_and_demo_subnets_differ() -> None:
    prod = load("docker-compose.prod.yml")["networks"]["default"]["ipam"]["config"][0]["subnet"]
    demo = load("docker-compose.demo.yml")["networks"]["default"]["ipam"]["config"][0]["subnet"]
    assert not ipaddress.ip_network(prod).overlaps(ipaddress.ip_network(demo))
