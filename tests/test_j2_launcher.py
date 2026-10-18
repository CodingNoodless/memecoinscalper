import json

import pytest

from tradeslave.launcher import j2


@pytest.fixture
def spec(tmp_path):
    (tmp_path / "logo.png").write_bytes(b"\x89PNG")
    d = {"name": "Test Coin", "symbol": "TEST", "description": "an original test token", "image": "logo.png",
         "original": True}
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(d))
    return p


def test_spec_ok_and_image_relative(spec):
    s = j2.Spec.load(spec)
    assert j2.check_spec(s) == []


def test_spec_guardrails(spec):
    s = j2.Spec.load(spec)
    s.original = False
    s.twitter = "https://x.com/someone"
    errs = " ".join(j2.check_spec(s))
    assert "original" in errs and "socials" in errs


def test_gate(monkeypatch):
    monkeypatch.delenv("J2_LIVE_APPROVED", raising=False)
    assert "J2_LIVE_APPROVED" in j2.gate([])
    monkeypatch.setenv("J2_LIVE_APPROVED", "1")
    assert j2.gate([]) is None
    closed = {"k": "launch", "status": "closed", "net_sol": 0.01}
    assert "complete" in j2.gate([closed] * j2.MAX_LAUNCHES)
    assert "stop" in j2.gate([{**closed, "net_sol": -0.3}, {**closed, "net_sol": -0.25}])
    assert "not closed" in j2.gate([{**closed, "status": "open"}])
    # creator-fee claims count toward the cumulative result
    assert j2.session([{**closed, "net_sol": -0.6}, {"k": "claim", "net_sol": 0.2}]) == (1, pytest.approx(-0.4))


def test_bodies():
    b = j2.create_body("OWNER", "MINT", j2.Spec("N", "S", "d", "x", True), "uri", 1.0)
    assert b["action"] == "create" and b["amount"] == 1.0 and b["denominatedInSol"] == "true" and b["pool"] == "pump"
    s = j2.sell_body("OWNER", "MINT")
    assert s["action"] == "sell" and s["amount"] == "100%" and s["denominatedInSol"] == "false"


def test_sign_roundtrip():
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction
    a, b = Keypair(), Keypair()
    msg = MessageV0.try_compile(a.pubkey(), [transfer(TransferParams(from_pubkey=a.pubkey(), to_pubkey=b.pubkey(),
                                                                     lamports=1))], [], Hash.default())
    raw = bytes(VersionedTransaction.populate(msg, [Keypair().sign_message(b"x")]))
    tx = VersionedTransaction.from_bytes(j2.sign(raw, [a]))
    assert tx.verify_with_results() == [True]
