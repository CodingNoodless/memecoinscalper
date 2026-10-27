"""tools/lab.py: forward-test verdicts and the registry."""

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("lab", Path(__file__).resolve().parent.parent / "tools" / "lab.py")
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


def test_verdicts():
    assert lab.verdict(lab.stats([-0.1] * 10 + [-0.05] * 15)) == "KILL"              # clearly losing at n 25
    assert lab.verdict(lab.stats([0.05, -0.05] * 30 + [0.0])) == "KILL"                # n 61, no edge
    good = [0.25, -0.15] * 60                                                          # n 120, t ~2.7
    assert lab.verdict(lab.stats(good)) == "PASS"
    assert lab.verdict(lab.stats(good), variants=10**9) == "CONTINUE"                  # luck hurdle for many variants
    lucky = [-0.1] * 99 + [50.0]                                                       # one moonshot carries it
    assert lab.verdict(lab.stats(lucky)) != "PASS"
    assert lab.verdict(lab.stats([0.1, -0.05] * 5)) == "CONTINUE"                      # too early to tell


def test_registry_and_brief(tmp_path, monkeypatch):
    monkeypatch.setattr(lab, "DATA", tmp_path)
    monkeypatch.setattr(lab, "REGISTRY", tmp_path / "lab" / "registry.jsonl")
    (tmp_path / "x1").mkdir()
    (tmp_path / "x1" / "trades.jsonl").write_text("\n".join(json.dumps({"ret": -0.2, "t_exit": i}) for i in range(30)))
    lab.main(["reg", "add", "x1", "fam", "test idea", "--variants", "5"])
    lab.main(["reg", "set", "x1", "forward", "--note", "started"])
    reg = lab.registry()
    assert reg["x1"]["status"] == "forward" and reg["x1"]["family"] == "fam" and len(reg["x1"]["history"]) == 2
    text = lab.brief(write=str(tmp_path))
    assert "| x1 | 30 |" in text and "KILL" in text and (tmp_path / "STATE.md").exists()
