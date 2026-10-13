import os
import subprocess
import sys
from pathlib import Path

from tradeslave import config

ROOT = Path(__file__).resolve().parent.parent


def test_clamp_records_warning():
    before = len(config.CONFIG_WARNINGS)
    assert config._clamped_max("X_TEST", 24.0, 2.0) == 2.0
    assert len(config.CONFIG_WARNINGS) == before + 1
    assert "X_TEST=24" in config.CONFIG_WARNINGS[-1]
    config.CONFIG_WARNINGS.pop()


def test_value_under_cap_is_unchanged_and_silent():
    before = len(config.CONFIG_WARNINGS)
    assert config._clamped_max("X_TEST", 1.5, 2.0) == 1.5
    assert len(config.CONFIG_WARNINGS) == before


def test_pinned_test_settings_loaded():
    assert config.MAX_HOLD_HOURS == 1.0
    assert config.LLM_MODE == "off"


def _import_config_with(**env):
    full_env = {**os.environ, **env}
    return subprocess.run(
        [sys.executable, "-c", "import tradeslave.config as c; print(c.LLM_MODE, c.MAX_HOLD_HOURS)"],
        cwd=ROOT, env=full_env, capture_output=True, text=True,
    )


def test_invalid_llm_mode_is_rejected():
    result = _import_config_with(LLM_MODE="sometimes")
    assert result.returncode != 0
    assert "LLM_MODE must be one of" in result.stderr


def test_max_hold_is_capped_at_two_hours():
    result = _import_config_with(MAX_HOLD_HOURS="24")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[1] == "2.0"
