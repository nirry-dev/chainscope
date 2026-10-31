"""Shared pytest fixtures — all offline, each test gets a fresh SQLite DB."""
import pytest

from chainscope import db, analyze, sources

# well-known addresses in the bundled samples
EXPLOITER = "0xb2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2"
INTERMEDIARY = "0xd4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4"
DEX_ROUTER = "0xc3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3"
LUMEN = "0xa1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"
ZENITH = "0xe5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    yield c
    c.close()


@pytest.fixture
def eth_case(conn):
    """eth_token_launder sample loaded into a fresh case."""
    cid = db.create_case(conn, "eth case")
    analyze.ingest(conn, cid, sources.load_sample("eth_token_launder"))
    return conn, cid


@pytest.fixture
def btc_case(conn):
    """btc_peel_cluster sample loaded into a fresh case."""
    cid = db.create_case(conn, "btc case")
    analyze.ingest(conn, cid, sources.load_sample("btc_peel_cluster"))
    return conn, cid
