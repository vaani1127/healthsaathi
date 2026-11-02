from collections.abc import Iterator

import pytest

from app.anchor.evm import EvmAnchor
from app.scripts import tamper_study
from tests.anchor.chain import Chain, deploy, start_anvil


@pytest.fixture(scope="module")
def anvil() -> Iterator[Chain]:
    chain = start_anvil()
    yield chain
    chain.stop()


def detected(results: dict[str, dict[str, object]]) -> set[str]:
    return {name for name, r in results.items() if r["detected"]}


async def test_which_checks_catch_each_attack(anvil: Chain) -> None:
    await deploy(anvil)
    evm = EvmAnchor(anvil.rpc_url, anvil.anchor, None, anvil.registry)
    try:
        control = await tamper_study.scenario(evm, anvil.admins, None)
        events = await tamper_study.scenario(evm, anvil.admins, "events")
        hidden = await tamper_study.scenario(evm, anvil.admins, "events_and_checkpoints")
    finally:
        await evm.close()
    assert detected(control) == set()
    assert detected(events) == {"audit_page", "next_checkpoint", "chain", "witness"}
    # With the stored checkpoints gone, only the outside witnesses still catch the rewrite.
    assert detected(hidden) == {"chain", "witness"}
    assert "3 of 3" in str(hidden["witness"]["detail"])
