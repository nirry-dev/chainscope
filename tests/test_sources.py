"""Dataset normalization and live-import input validation (offline)."""
import pytest

from chainscope import sources


def test_validate_eth_ok_lowercases():
    a = sources.validate_address("0xABCdef0000000000000000000000000000000001", "eth")
    assert a == "0xabcdef0000000000000000000000000000000001"


@pytest.mark.parametrize("bad", ["0xZZZ", "0x123", "nope", ""])
def test_validate_eth_rejects(bad):
    with pytest.raises(ValueError):
        sources.validate_address(bad, "eth")


def test_validate_btc():
    assert sources.validate_address("bc1qactor00000000000000000000000000dep001", "btc")
    with pytest.raises(ValueError):
        sources.validate_address("hello", "btc")


def test_normalize_drops_bad_transfers_keeps_extras():
    d = sources.normalize_dataset({
        "chain": "eth",
        "prices": {"ETH": "3400", "BAD": "x"},
        "cospends": [["a", "b"], ["solo"]],
        "transfers": [
            {"txid": "t1", "src": "a", "dst": "b", "value": "1.5"},
            {"txid": "t2", "src": "a"},          # missing dst -> dropped
            {"src": "a", "dst": "b"},            # missing txid -> dropped
        ],
    })
    assert len(d["transfers"]) == 1
    assert d["transfers"][0]["value"] == 1.5
    assert d["prices"] == {"ETH": 3400.0}        # non-numeric dropped
    assert d["cospends"] == [["a", "b"]]         # single-member group dropped


def test_clamp_txs():
    assert sources._clamp_txs("abc") == 25       # bad -> default
    assert sources._clamp_txs(99999) == 500      # capped
    assert sources._clamp_txs(-3) == 1           # floored


def test_fetch_evm_requires_key_and_base():
    with pytest.raises(ValueError):
        sources.fetch_evm("0x" + "a" * 40, "https://api.example", "")
    with pytest.raises(ValueError):
        sources.fetch_evm("0x" + "a" * 40, "", "key")
