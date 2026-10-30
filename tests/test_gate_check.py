import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gate_check", ROOT / "tools" / "gate_check.py")
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)
CFG = yaml.safe_load((ROOT / "lab" / "gates.yaml").read_text(encoding="utf-8"))


def good_series(n=600, seed=1):
    # many small wins, capped losses: +6% mean, no single outlier carries it
    rng = np.random.default_rng(seed)
    return list(np.where(rng.random(n) < 0.6, rng.uniform(0.08, 0.25, n), rng.uniform(-0.12, -0.02, n)))


def bad_series(n=600, seed=2):
    # memecoin-like: mostly losses, a few huge winners, negative after removing the top 5%
    rng = np.random.default_rng(seed)
    r = rng.uniform(-0.30, 0.02, n)
    r[: n // 50] = rng.uniform(3, 8, n // 50)
    return list(r)


def results(r, shifts=None, final=False, trials=10):
    return {g: res for g, _, _, res in gc.evaluate(r, CFG, trials, shifts, final)}


def test_locked_gate_values():
    assert CFG["min_trades_oos"] == 300 and CFG["profit_factor_after_costs"] == 1.3
    assert CFG["bootstrap_resamples"] >= 10_000 and CFG["max_drawdown"] == 0.35
    assert CFG["param_stability_pf"] == 1.1 and CFG["outlier_top_share"] == 0.05
    assert CFG["deflated_sharpe_prob"] == 0.95 and CFG["holdout_profit_factor"] == 1.2


def test_known_good_passes_every_gate():
    r = good_series()
    shifts = {"shift_a_up": good_series(seed=3), "shift_a_down": good_series(seed=4)}
    res = results(r, shifts, final=True)
    assert all(v == "PASS" for v in res.values()), res


def test_known_bad_fails():
    r = bad_series()
    res = results(r, {"shift_a_up": bad_series(seed=5)}, final=True)
    assert res["outlier_test"] == "FAIL"
    assert res["expectancy_ci95_lower"] == "FAIL" or res["deflated_sharpe_prob"] == "FAIL"
    assert any(v == "FAIL" for v in res.values())


def test_missing_shifts_and_small_n_fail():
    res = results(good_series(n=100), shifts=None)
    assert res["min_trades_oos"] == "FAIL" and res["param_stability"] == "FAIL"
    assert res["holdout_profit_factor"].startswith("PENDING")


def test_more_trials_lower_dsr():
    r = list(np.random.default_rng(7).normal(0.01, 0.2, 400))
    assert gc.deflated_sharpe(r, 1000) < gc.deflated_sharpe(r, 1)


def test_drawdown_and_pf():
    assert gc.max_drawdown([-1.0] * 5, 0.1) == pytest.approx(1 - 0.9 ** 5)
    assert gc.profit_factor([0.2, -0.1]) == pytest.approx(2.0)


def test_default_mode_refuses_holdout(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, "gates_hash", lambda *a: "test")
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(json.dumps({"ret": 0.1, "split": s}) for s in ("train", "holdout")))
    with pytest.raises(SystemExit, match="holdout"):
        gc.main([str(f)])
    h = tmp_path / "holdout.jsonl"
    h.write_text(json.dumps({"ret": 0.1}))
    with pytest.raises(SystemExit, match="holdout"):
        gc.main([str(h)])


def test_final_is_one_shot(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, "gates_hash", lambda *a: "test")
    monkeypatch.setattr(gc, "HOLDOUT_LOG", tmp_path / "holdout_log.jsonl")
    monkeypatch.setattr(gc, "trials_for", lambda *a: (1, "test"))
    f = tmp_path / "holdout.jsonl"
    f.write_text("\n".join(json.dumps({"ret": x}) for x in good_series()))
    gc.main(["--final", "X1", str(f)])
    assert json.loads((tmp_path / "holdout_log.jsonl").read_text())["id"] == "X1"
    with pytest.raises(SystemExit, match="already used"):
        gc.main(["--final", "X1", str(f)])
