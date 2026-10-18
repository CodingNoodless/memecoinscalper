"""J2 live micro-test: launch a token with a dev buy in the creation transaction, sell the whole bag
HOLD_S seconds later, collect the 0.30% creator fee. Vault: "J - Creator launches", Roadmap stage J-2.

    uv run python -m tradeslave.launcher.j2 status
    uv run python -m tradeslave.launcher.j2 simulate --spec launches/my_token.json   # no SOL spent
    uv run python -m tradeslave.launcher.j2 launch   --spec launches/my_token.json   # live, guarded
    uv run python -m tradeslave.launcher.j2 claim                                     # creator fees

Execution goes through PumpPortal's Local Transaction API (pumpportal.fun/api/trade-local): it builds
the transaction, we sign locally and send through our own RPC. PumpPortal charges 0.5% per trade
(sourced: pumpportal.fun/fees), on top of the 1.25% curve fee. Keys never leave this machine.

Guardrails (all enforced in code):
- Live actions need J2_LIVE_APPROVED=1 in .env (Rohit sets it after the legal/tax check) and --yes.
- At most MAX_LAUNCHES launches and a cumulative stop at STOP_SOL (ledger: data/j2_live.jsonl).
- One wallet, one buy (inside the create transaction), then only sells: no wash trading, no
  extra wallets, no bundles. The spec must state the token is original and that any social link
  belongs to us (no impersonation, no fake socials).

.env names: J2_WALLET_KEYPAIR (path to a solana-keygen JSON file), PINATA_JWT (metadata upload),
RESEARCH_RPC_URL (send/confirm), J2_DEV_BUY_SOL (default 1.0), J2_LIVE_APPROVED.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .. import config  # noqa: F401  (loads .env)
from ..paths import DATA_DIR

PORTAL = "https://pumpportal.fun/api/trade-local"
PINATA = "https://uploads.pinata.cloud/v3/files"
GATEWAY = "https://ipfs.io/ipfs/"
LEDGER = DATA_DIR / "j2_live.jsonl"

HOLD_S = 15                 # J2 rule: sell at the first outside trade >= 15 s after creation
MAX_LAUNCHES = 10
STOP_SOL = -0.5             # cumulative net SOL; at 1 SOL dev buys a dead launch costs ~0.07 SOL
SLIPPAGE_PCT = 15
PRIORITY_SOL = 0.0005
LAMPORTS = 1e9


def dev_buy_sol() -> float:
    return float(os.getenv("J2_DEV_BUY_SOL", "1.0"))


# ---------------------------------------------------------------- spec and guardrails

@dataclass
class Spec:
    name: str
    symbol: str
    description: str
    image: str
    original: bool = False
    socials_are_ours: bool = False
    website: str = ""
    twitter: str = ""
    telegram: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "Spec":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        base = Path(path).parent
        spec = cls(**{k: d[k] for k in d if k in cls.__dataclass_fields__})
        if spec.image and not Path(spec.image).is_absolute():
            spec.image = str(base / spec.image)
        return spec


def check_spec(s: Spec) -> list[str]:
    errs = []
    if not 1 <= len(s.name) <= 32:
        errs.append("name must be 1-32 characters")
    if not 1 <= len(s.symbol) <= 10:
        errs.append("symbol must be 1-10 characters")
    if not s.description.strip():
        errs.append("description is required")
    if not Path(s.image).is_file():
        errs.append(f"image not found: {s.image}")
    if not s.original:
        errs.append('set "original": true (the token does not copy or impersonate a real person, brand or token)')
    if (s.website or s.twitter or s.telegram) and not s.socials_are_ours:
        errs.append('social links must be ours: set "socials_are_ours": true, or remove them')
    return errs


def ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    return [json.loads(x) for x in LEDGER.read_text(encoding="utf-8").splitlines() if x.strip()]


def log(row: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def session(rows: list[dict]) -> tuple[int, float]:
    """(launches done, cumulative net SOL including creator-fee claims)."""
    launches = [r for r in rows if r.get("k") == "launch"]
    net = sum(r.get("net_sol") or 0.0 for r in rows if r.get("k") in ("launch", "claim"))
    return len(launches), net


def gate(rows: list[dict]) -> str | None:
    """Reason to refuse a new launch, or None."""
    if os.getenv("J2_LIVE_APPROVED") != "1":
        return "J2_LIVE_APPROVED=1 is not set in .env (needs Rohit's go after the legal/tax check)"
    n, net = session(rows)
    if n >= MAX_LAUNCHES:
        return f"micro-test complete: {n}/{MAX_LAUNCHES} launches"
    if net <= STOP_SOL:
        return f"stop hit: cumulative {net:+.3f} SOL <= {STOP_SOL}"
    if any(r.get("k") == "launch" and r.get("status") != "closed" for r in rows):
        return "a previous launch is not closed (check the ledger and sell manually first)"
    return None


# ---------------------------------------------------------------- chain plumbing

def keypair():
    from solders.keypair import Keypair
    p = os.getenv("J2_WALLET_KEYPAIR")
    if not p or not Path(p).is_file():
        sys.exit("J2_WALLET_KEYPAIR must point to a solana-keygen JSON keypair file")
    return Keypair.from_bytes(bytes(json.loads(Path(p).read_text())))


def rpc_url() -> str:
    u = os.getenv("RESEARCH_RPC_URL")
    if not u:
        sys.exit("RESEARCH_RPC_URL is not set")
    return u


def rpc(method: str, params: list):
    r = requests.post(rpc_url(), json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(f"{method}: {d['error']}")
    return d["result"]


def balance(pub) -> float:
    return rpc("getBalance", [str(pub), {"commitment": "confirmed"}])["value"] / LAMPORTS


def portal(body: dict) -> bytes:
    r = requests.post(PORTAL, json=body, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"PumpPortal {r.status_code}: {r.text[:300]}")
    return r.content


def sign(raw: bytes, signers: list) -> bytes:
    from solders.transaction import VersionedTransaction
    tx = VersionedTransaction.from_bytes(raw)
    return bytes(VersionedTransaction(tx.message, signers))


def simulate(tx: bytes) -> dict:
    return rpc("simulateTransaction", [base64.b64encode(tx).decode(),
                                       {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
                                        "commitment": "confirmed"}])["value"]


def send(tx: bytes) -> str:
    return rpc("sendTransaction", [base64.b64encode(tx).decode(),
                                   {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}])


def confirm(sig: str, timeout: float = 45) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        st = rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": False}])["value"][0]
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            if st.get("err"):
                raise RuntimeError(f"transaction failed: {st['err']}")
            return st
        time.sleep(0.4)
    raise TimeoutError(f"not confirmed in {timeout:.0f} s: {sig}")


def block_time(slot: int) -> float | None:
    try:
        return float(rpc("getBlockTime", [slot]))
    except Exception:  # noqa: BLE001 - block time can lag; caller falls back to the local clock
        return None


def upload_metadata(s: Spec) -> str:
    jwt = os.getenv("PINATA_JWT")
    if not jwt:
        sys.exit("PINATA_JWT is not set (free Pinata account; pump.fun's own IPFS endpoint is retired)")
    h = {"Authorization": f"Bearer {jwt}"}
    with open(s.image, "rb") as fh:
        r = requests.post(PINATA, headers=h, files={"file": fh}, data={"network": "public"}, timeout=60)
    r.raise_for_status()
    meta = {"name": s.name, "symbol": s.symbol, "description": s.description, "image": GATEWAY + r.json()["data"]["cid"],
            "showName": True, "createdOn": "https://pump.fun"}
    for k in ("website", "twitter", "telegram"):
        if getattr(s, k):
            meta[k] = getattr(s, k)
    r = requests.post(PINATA, headers=h, data={"network": "public"},
                      files={"file": ("metadata.json", json.dumps(meta), "application/json")}, timeout=60)
    r.raise_for_status()
    return GATEWAY + r.json()["data"]["cid"]


def create_body(owner, mint, s: Spec, uri: str, size: float) -> dict:
    return {"publicKey": str(owner), "action": "create", "tokenMetadata": {"name": s.name, "symbol": s.symbol, "uri": uri},
            "mint": str(mint), "denominatedInSol": "true", "amount": size, "slippage": SLIPPAGE_PCT,
            "priorityFee": PRIORITY_SOL, "pool": "pump"}


def sell_body(owner, mint, slippage: int = SLIPPAGE_PCT) -> dict:
    return {"publicKey": str(owner), "action": "sell", "mint": str(mint), "denominatedInSol": "false",
            "amount": "100%", "slippage": slippage, "priorityFee": PRIORITY_SOL, "pool": "pump"}


# ---------------------------------------------------------------- commands

def cmd_status() -> None:
    rows = ledger()
    n, net = session(rows)
    for r in rows:
        print(json.dumps(r))
    print(f"\nlaunches {n}/{MAX_LAUNCHES}, cumulative {net:+.4f} SOL (stop {STOP_SOL}), dev buy {dev_buy_sol()} SOL")
    print(f"gate: {gate(rows) or 'open'}")


def cmd_simulate(spec_path: str) -> None:
    from solders.keypair import Keypair
    s = Spec.load(spec_path)
    errs = check_spec(s)
    print("spec:", "ok" if not errs else "; ".join(errs))
    owner = keypair()
    mint = Keypair()
    size = dev_buy_sol()
    bal = balance(owner.pubkey())
    print(f"wallet {owner.pubkey()} balance {bal:.4f} SOL; dev buy {size} SOL")
    raw = portal(create_body(owner.pubkey(), mint.pubkey(), s, "https://example.invalid/metadata.json", size))
    res = simulate(sign(raw, [mint, owner]))
    print(f"create simulation: err={res.get('err')} units={res.get('unitsConsumed')}")
    for line in (res.get("logs") or [])[-6:]:
        print("   ", line)
    print(f"gate for a live launch: {gate(ledger()) or 'open'}")


def cmd_launch(spec_path: str, yes: bool) -> None:
    from solders.keypair import Keypair
    rows = ledger()
    if reason := gate(rows):
        sys.exit(f"refused: {reason}")
    s = Spec.load(spec_path)
    if errs := check_spec(s):
        sys.exit("refused: " + "; ".join(errs))
    if not yes:
        sys.exit("refused: pass --yes to spend real SOL")
    owner, mint, size = keypair(), Keypair(), dev_buy_sol()
    bal0 = balance(owner.pubkey())
    if bal0 < size + 0.1:
        sys.exit(f"refused: balance {bal0:.4f} SOL < dev buy {size} + 0.1 SOL for rent and fees")
    uri = upload_metadata(s)
    row = {"k": "launch", "t": time.time(), "mint": str(mint.pubkey()), "symbol": s.symbol, "dev_buy": size,
           "bal0": bal0, "status": "creating"}
    tx = sign(portal(create_body(owner.pubkey(), mint.pubkey(), s, uri, size)), [mint, owner])
    t_send = time.time()
    try:
        row["create_sig"] = send(tx)
        st = confirm(row["create_sig"])
        row["create_slot"] = st["slot"]
        t_create = block_time(st["slot"]) or t_send
        row["status"] = "open"
        print(f"created {row['mint']} in slot {st['slot']} ({time.time() - t_send:.1f} s after send)")
        time.sleep(max(0.0, t_create + HOLD_S - time.time()))
        for k, slip in enumerate((SLIPPAGE_PCT, 30, 50, 90, 99)):
            try:
                sig = send(sign(portal(sell_body(owner.pubkey(), mint.pubkey(), slip)), [owner]))
                confirm(sig)
                row.update(sell_sig=sig, sell_after_s=round(time.time() - t_create, 1), status="closed")
                break
            except Exception as exc:  # noqa: BLE001 - retry with more slippage, then leave it open for a manual sell
                print(f"sell attempt {k + 1} failed: {exc}")
                time.sleep(1)
    except Exception as exc:  # noqa: BLE001 - a failed create spends only fees; record it either way
        row["error"] = str(exc)[:300]
        if row["status"] == "creating":
            row["status"] = "closed"           # nothing was bought
    finally:
        row["bal1"] = balance(owner.pubkey())
        row["net_sol"] = row["bal1"] - bal0
        log(row)
    print(f"{row['status']}: net {row['net_sol']:+.4f} SOL (rent, fees and PumpPortal 0.5% included; "
          f"creator fees are collected with `claim`)")


def cmd_claim(yes: bool) -> None:
    if os.getenv("J2_LIVE_APPROVED") != "1" or not yes:
        sys.exit("refused: needs J2_LIVE_APPROVED=1 and --yes")
    owner = keypair()
    bal0 = balance(owner.pubkey())
    sig = send(sign(portal({"publicKey": str(owner.pubkey()), "action": "collectCreatorFee",
                            "priorityFee": PRIORITY_SOL, "pool": "pump"}), [owner]))
    confirm(sig)
    net = balance(owner.pubkey()) - bal0
    log({"k": "claim", "t": time.time(), "sig": sig, "net_sol": net})
    print(f"claimed creator fees: {net:+.4f} SOL")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["status", "simulate", "launch", "claim"])
    ap.add_argument("--spec")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    if a.cmd in ("simulate", "launch") and not a.spec:
        sys.exit("--spec is required")
    {"status": cmd_status, "simulate": lambda: cmd_simulate(a.spec), "launch": lambda: cmd_launch(a.spec, a.yes),
     "claim": lambda: cmd_claim(a.yes)}[a.cmd]()


if __name__ == "__main__":
    main()
