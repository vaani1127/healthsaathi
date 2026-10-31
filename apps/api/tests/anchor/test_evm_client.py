from typing import Any

from app.anchor.evm import EvmAnchor

ADDRESS = "0x" + "11" * 20


async def test_parses_a_long_extra_data_block() -> None:
    """Amoy blocks carry 97+ bytes of extraData; web3 refuses them without the POA middleware."""
    evm = EvmAnchor("http://127.0.0.1:1", ADDRESS, None)

    async def fake(method: str, params: Any) -> dict[str, Any]:
        block = {"number": "0x1", "extraData": "0x" + "ab" * 104, "baseFeePerGas": "0x1"}
        return {"jsonrpc": "2.0", "id": 1, "result": block}

    evm.w3.provider.make_request = fake  # type: ignore[assignment]
    block = await evm.w3.eth.get_block("latest")
    assert block["number"] == 1
