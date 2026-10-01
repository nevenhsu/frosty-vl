import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import craft_prompt_rules


def test_load_craft_prompt_rules_returns_verified_payload():
    rules = craft_prompt_rules.load_craft_prompt_rules()

    assert set(rules) == {"id", "version", "revision", "digest", "instructions"}
    assert rules["id"] == "qwen-image-gen"
    assert rules["version"] == "0.2.1"
    assert rules["revision"] == "01c8ffe0bc94cfe1dbfc7e1de537399253d04f39"
    assert rules["digest"] == "2131c9ad4c5ba273f65cbd39260004c2a7d5e57020b463005f1b3a3ffe3c99cd"
    assert "# Qwen Image Gen" in rules["instructions"]
    assert "# 按任务改写" in rules["instructions"]
    assert "# 实测依据与适用范围" in rules["instructions"]


def test_load_craft_prompt_rules_rejects_changed_instruction(tmp_path, monkeypatch):
    bundle = tmp_path / "qwen-image-gen"
    shutil.copytree(craft_prompt_rules.BUNDLE_DIR, bundle)
    target = bundle / "references" / "rewriting.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    monkeypatch.setattr(craft_prompt_rules, "BUNDLE_DIR", bundle)

    with pytest.raises(ValueError, match="digest mismatch"):
        craft_prompt_rules.load_craft_prompt_rules()
