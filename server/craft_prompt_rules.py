"""Load the pinned Qwen Image prompt-writing rules.

The vendored skill is deliberately kept as upstream files.  The manifest tells
the loader which instruction files make up the supplemental prompt, while the
digests below pin those files to the reviewed source commit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any


BUNDLE_DIR = (
    Path(__file__).resolve().parent / "vendor" / "craft_skills" / "qwen-image-gen"
)
PINNED_REVISION = "01c8ffe0bc94cfe1dbfc7e1de537399253d04f39"

_EXPECTED_ID = "qwen-image-gen"
_EXPECTED_VERSION = "0.2.1"
_EXPECTED_INSTRUCTION_FILES = (
    "SKILL.md",
    "references/rewriting.md",
    "references/tested-behavior.md",
)

# These hashes are SHA-256 values of the files at PINNED_REVISION.  The
# manifest and license are checked too, so the complete vendored boundary is
# covered even though only the manifest-declared instruction files are joined.
_EXPECTED_FILE_DIGESTS = {
    "SKILL.md": "1d5d16ecef842cde03c2e57a30508e9da9d71607db42c4ae4e60c4aa7155e5d4",
    "references/rewriting.md": "1f52f724ed23a37eaf6c224326c1f2090982967950b0ec439c5abd746ce01e7f",
    "references/tested-behavior.md": "26a641103e80aea4abfe52081d4529338c95b232f39cfb3d6323ad5c993086d5",
    "runtime/manifest.json": "16e70bba8fd49a21a5b713632dc3ae6660cc1faaeccfc52d3fea27af85cb521d",
    "LICENSE": "247d6cf1151b9dbfef39f7edc11c939e80668f8c10321a7286ba2748f354d86a",
}


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("qwen-image-gen manifest has an invalid instruction file")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("qwen-image-gen manifest contains an unsafe instruction path")
    return value


def _bundle_digest(digests: dict[str, str]) -> str:
    # Include names as well as hashes so a file cannot be renamed without
    # changing the reported rules digest.  The manifest is part of this digest
    # because it defines the rule set and version metadata.
    paths = ("runtime/manifest.json",) + _EXPECTED_INSTRUCTION_FILES
    canonical = "\n".join(f"{path}:{digests[path]}" for path in paths)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def load_craft_prompt_rules() -> dict[str, str]:
    """Return the verified rules as a supplemental system-prompt payload.

    The returned mapping contains exactly ``id``, ``version``, ``revision``,
    ``digest`` and ``instructions``.  Missing files, malformed metadata and
    any pin or digest mismatch raise ``OSError`` or ``ValueError``.
    """

    manifest_path = BUNDLE_DIR / "runtime" / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    if not hmac.compare_digest(
        hashlib.sha256(manifest_bytes).hexdigest(),
        _EXPECTED_FILE_DIGESTS["runtime/manifest.json"],
    ):
        raise ValueError("qwen-image-gen manifest digest mismatch")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("qwen-image-gen manifest is not valid UTF-8 JSON") from exc

    if not isinstance(manifest, dict):
        raise ValueError("qwen-image-gen manifest must be an object")
    if manifest.get("schemaVersion") != 1:
        raise ValueError("qwen-image-gen manifest schema mismatch")
    if manifest.get("id") != _EXPECTED_ID:
        raise ValueError("qwen-image-gen manifest id mismatch")
    if manifest.get("version") != _EXPECTED_VERSION:
        raise ValueError("qwen-image-gen manifest version mismatch")
    declared_files = manifest.get("instructionFiles")
    if not isinstance(declared_files, list):
        raise ValueError("qwen-image-gen manifest instructionFiles must be a list")
    safe_files = tuple(_safe_relative_path(value) for value in declared_files)
    if safe_files != _EXPECTED_INSTRUCTION_FILES:
        raise ValueError("qwen-image-gen manifest instruction files mismatch")

    digests: dict[str, str] = {"runtime/manifest.json": _EXPECTED_FILE_DIGESTS["runtime/manifest.json"]}
    instruction_text: list[str] = []
    for relative_path in safe_files:
        path = BUNDLE_DIR / relative_path
        digest = _sha256(path)
        expected = _EXPECTED_FILE_DIGESTS[relative_path]
        if not hmac.compare_digest(digest, expected):
            raise ValueError(f"qwen-image-gen digest mismatch: {relative_path}")
        digests[relative_path] = digest
        instruction_text.append(path.read_text(encoding="utf-8"))

    license_path = BUNDLE_DIR / "LICENSE"
    if not hmac.compare_digest(_sha256(license_path), _EXPECTED_FILE_DIGESTS["LICENSE"]):
        raise ValueError("qwen-image-gen license digest mismatch")

    return {
        "id": _EXPECTED_ID,
        "version": _EXPECTED_VERSION,
        "revision": PINNED_REVISION,
        "digest": _bundle_digest(digests),
        "instructions": "\n\n".join(instruction_text),
    }
