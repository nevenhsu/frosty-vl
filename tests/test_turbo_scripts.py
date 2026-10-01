"""Focused, no-model checks for the portable Apple Silicon launchers."""
from __future__ import annotations

import os
import hashlib
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup-qwen21-macos.command"
START = ROOT / "start-frosty-qwen21"
COMFY_LAUNCHER = ROOT / "scripts/start-comfyui-qwen21.command"
IMAGE_LAUNCHER = ROOT / "scripts/start-comfy-image.sh"


def run_script(path: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(path), *args],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )


def test_shell_launchers_parse():
    for path in (SETUP, START, COMFY_LAUNCHER, IMAGE_LAUNCHER):
        result = subprocess.run(["/bin/bash", "-n", str(path)], text=True, capture_output=True, check=False)
        assert result.returncode == 0, (path, result.stderr)


def test_help_selects_turbo_profile_without_touching_install():
    env = os.environ.copy()
    env["FVL_QWEN21_PROFILE"] = "turbo"
    result = run_script(SETUP, "--help", env=env)
    assert result.returncode == 0
    assert "Profile: turbo" in result.stdout
    assert "[--base|--turbo]" in result.stdout


def test_all_launchers_default_to_turbo_and_keep_explicit_base():
    env = os.environ.copy()
    env.pop("FVL_QWEN21_PROFILE", None)
    for path in (SETUP, START, COMFY_LAUNCHER, IMAGE_LAUNCHER):
        result = run_script(path, "--help", env=env)
        assert result.returncode == 0, (path, result.stderr)
        assert "Profile: turbo" in result.stdout, path
        result = run_script(path, "--base", "--help", env=env)
        assert result.returncode == 0, (path, result.stderr)
        assert "Profile: base" in result.stdout, path


def test_check_only_does_not_create_download_lock(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "uname").write_text("#!/bin/sh\n[ \"$1\" = -s ] && echo Darwin || echo arm64\n")
    (fake_bin / "id").write_text("#!/bin/sh\n[ \"$1\" = -u ] && echo 501 || /usr/bin/id \"$@\"\n")
    (fake_bin / "xcode-select").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "xcrun").write_text("#!/bin/sh\nexit 0\n")
    for path in fake_bin.iterdir():
        path.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["FVL_QWEN21_HOME"] = str(tmp_path / "install")
    result = run_script(SETUP, "--base", "--check-only", env=env)
    assert result.returncode != 0
    assert not (tmp_path / "install" / "qwen21-downloads").exists()


def test_turbo_runtime_checks_profile_and_object_info():
    text = START.read_text()
    assert "data.get(\"profile\") == sys.argv[1]" in text
    assert "/object_info" in text
    for node in ("FrostyViggleTurboLora", "FrostyViggleTurboSigmas", "BasicGuider",
                 "RandomNoise", "KSamplerSelect", "SamplerCustomAdvanced"):
        assert node in text
    assert "Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors" in text


def test_existing_adapter_profile_mismatch_stops_before_reuse(tmp_path):
    install = tmp_path / "install"
    comfy = tmp_path / "ComfyUI"
    models = tmp_path / "models"
    (comfy / ".venv/bin").mkdir(parents=True)
    (comfy / "custom_nodes/ComfyUI-GGUF").mkdir(parents=True)
    (comfy / "custom_nodes/frosty_viggle_turbo_v021").mkdir(parents=True)
    (comfy / "main.py").touch()
    comfy_python = comfy / ".venv/bin/python"
    comfy_python.write_text("#!/bin/sh\nexit 0\n")
    comfy_python.chmod(0o755)
    (comfy / "custom_nodes/ComfyUI-GGUF/__init__.py").touch()
    (comfy / "custom_nodes/frosty_viggle_turbo_v021/__init__.py").touch()
    (comfy / "custom_nodes/frosty_viggle_turbo_v021/viggle_turbo.py").touch()
    for relative in (
        "diffusion_models/qwen-image-2.1-Q4_K_M.gguf",
        "text_encoders/qwen3vl_8b_int8_convrot.safetensors",
        "vae/qwen_image_2.1_vae_bf16.safetensors",
        "loras/Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors",
    ):
        path = models / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    real_python = os.environ.get("PYTHON", "/usr/bin/python3")
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = -c ] && printf '%s' \"$2\" | grep -q json.load; then\n"
        f"  exec {real_python} \"$@\"\n"
        "fi\n"
        "exit 0\n"
    )
    fake_python.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *8890/image*) exit 1 ;;\n"
        "  *8899/health*) printf '%s' '{\"ready\":true,\"profile\":\"base\"}' ;;\n"
        "  *8188/system_stats*) printf '%s' '{\"system\":{\"comfyui_version\":\"stub\"}}' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    fake_curl.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "FVL_QWEN21_HOME": str(install),
        "FVL_COMFYUI_DIR": str(comfy),
        "FVL_QWEN21_MODEL_DIR": str(models),
        "FVL_COMFY_PYTHON": str(fake_python),
        "FVL_QWEN21_PROFILE": "turbo",
        "FVL_NO_BROWSER": "1",
    })
    result = run_script(START, "--turbo", env=env)
    assert result.returncode != 0
    assert "uses profile 'base'; requested 'turbo'" in result.stderr


def _executable(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o755)
    return path


def _setup_rerun_fixture(tmp_path: Path) -> dict[str, object]:
    """Build a tiny, fully local copy of setup for two-run idempotence checks."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    setup_copy = bundle / "setup-qwen21-macos.command"
    setup_text = SETUP.read_text()
    payloads = [b"base0", b"base11", b"base222", b"lora3333"]
    sizes = "model_size=(5 6 7)"
    hashes = "\n".join(f'  "{hashlib.sha256(payload).hexdigest()}"' for payload in payloads[:3])
    setup_text = setup_text.replace("model_size=(4604557984 9350798360 675509688)", sizes)
    setup_text = setup_text.replace(
        '  "833439e91bc1152d28f37aa198c7f6f4218b7de95754c2f7a318a2422ab4b2f8"\n'
        '  "8bfd0f6e12abf2d2d697ecc888e5e90b0d6741d6708f05799f53afa560452e8f"\n'
        '  "bb21f7473051e1ac368515dd3f2e15cd44d7a11748ee8823e1ddca3e4876b7c9"',
        hashes,
    )
    setup_text = setup_text.replace(
        'model_size+=(679604800)',
        'model_size+=(8)',
    ).replace(
        'model_sha+=("bafb91d0047df3f9b8a5a850b0c967f051164314d8aad778dfa34d9c24ec345b")',
        f'model_sha+=("{hashlib.sha256(payloads[3]).hexdigest()}")',
    )
    setup_copy.write_text(setup_text)
    setup_copy.chmod(0o755)

    scripts = bundle / "scripts"
    scripts.mkdir()
    shutil.copy2(ROOT / "start-frosty-qwen21", bundle / "start-frosty-qwen21")
    shutil.copy2(ROOT / "scripts/start-comfyui-qwen21.command", scripts / "start-comfyui-qwen21.command")
    shutil.copy2(ROOT / "scripts/start-comfy-image.sh", scripts / "start-comfy-image.sh")
    (bundle / "requirements-comfy.txt").write_text("")
    (bundle / "config").mkdir()
    (bundle / "config/comfyui-image-turbo.json").write_text("{}")
    shutil.copytree(ROOT / "comfyui/frosty_viggle_turbo_v021", bundle / "comfyui/frosty_viggle_turbo_v021")

    install = tmp_path / "install"
    model_dir = install / "qwen21-downloads/models"
    model_dir.mkdir(parents=True)
    external = tmp_path / "verified-gguf"
    external.write_bytes(payloads[0])
    (model_dir / "diffusion_models").mkdir()
    (model_dir / "diffusion_models/qwen-image-2.1-Q4_K_M.gguf").symlink_to(external)
    for relative, payload in zip(
        (
            "text_encoders/qwen3vl_8b_int8_convrot.safetensors",
            "vae/qwen_image_2.1_vae_bf16.safetensors",
            "loras/Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors",
        ),
        payloads[1:],
    ):
        path = model_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    comfy = install / "ComfyUI"
    (comfy / ".venv/bin").mkdir(parents=True)
    (comfy / "main.py").write_text("# existing ComfyUI source\n")
    _executable(comfy / ".venv/bin/python", "#!/bin/sh\nprintf '%s\\n' \"comfy $*\" >> \"$STUB_LOG\"\ncase \"$*\" in *'-m pip'*|*'-m venv'*) exit 91;; esac\nexit 0\n")
    (comfy / ".venv/pyvenv.cfg").write_text("existing-pyvenv\n")
    (comfy / ".venv/settings").write_text("existing-comfy-settings\n")
    (comfy / "custom_nodes/ComfyUI-GGUF").mkdir(parents=True)
    (comfy / "custom_nodes/ComfyUI-GGUF/__init__.py").write_text("existing-gguf\n")
    extra_model_paths = comfy / "extra_model_paths.yaml"
    extra_model_paths.write_text("existing-model-paths\n")

    adapter = bundle / ".venv-comfy/bin/python"
    adapter.parent.mkdir(parents=True)
    _executable(adapter, "#!/bin/sh\nprintf '%s\\n' \"adapter $*\" >> \"$STUB_LOG\"\ncase \"$*\" in *'-m pip'*|*'-m venv'*) exit 92;; esac\nexit 0\n")
    model_paths = install / "qwen21-downloads/comfyui_model_paths.yaml"
    model_paths.write_text("preserve-this-file\n")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(fake_bin / "uname", "#!/bin/sh\n[ \"$1\" = -s ] && echo Darwin || echo arm64\n")
    _executable(fake_bin / "id", "#!/bin/sh\n[ \"$1\" = -u ] && echo 501 || /usr/bin/id \"$@\"\n")
    _executable(fake_bin / "xcode-select", "#!/bin/sh\nexit 0\n")
    _executable(fake_bin / "xcrun", "#!/bin/sh\nexit 0\n")
    _executable(
        fake_bin / "stat",
        "#!/bin/sh\n"
        "follow=0\npath=\"\"\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -L) follow=1; shift ;;\n"
        "    -f) shift 2 ;;\n"
        "    *) path=\"$1\"; shift ;;\n"
        "  esac\n"
        "done\n"
        "printf 'stat follow=%s path=%s\\n' \"$follow\" \"$path\" >> \"$STUB_LOG\"\n"
        "if [ \"$follow\" -eq 0 ] && [ -L \"$path\" ]; then echo 0; else wc -c < \"$path\" | tr -d '[:space:]'; fi\n",
    )
    _executable(fake_bin / "curl", "#!/bin/sh\nprintf 'curl %s\\n' \"$*\" >> \"$STUB_LOG\"\nexit 97\n")
    _executable(fake_bin / "git", "#!/bin/sh\nprintf 'git %s\\n' \"$*\" >> \"$STUB_LOG\"\nexit 98\n")
    _executable(fake_bin / "brew", "#!/bin/sh\nprintf 'brew %s\\n' \"$*\" >> \"$STUB_LOG\"\nexit 99\n")
    log = tmp_path / "stub.log"
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "FVL_QWEN21_HOME": str(install),
        "FVL_QWEN21_PROFILE": "turbo",
        "STUB_LOG": str(log),
    })
    return {
        "bundle": bundle,
        "install": install,
        "models": model_dir,
        "external": external,
        "model_paths": model_paths,
        "comfy_settings": comfy / ".venv/settings",
        "comfy_main": comfy / "main.py",
        "comfy_python": comfy / ".venv/bin/python",
        "comfy_pyvenv": comfy / ".venv/pyvenv.cfg",
        "comfy_model_paths": extra_model_paths,
        "env": env,
        "log": log,
    }


def _snapshot(path: Path) -> tuple[bool, str | None, bytes, int]:
    stat = path.lstat()
    target = str(path.readlink()) if path.is_symlink() else None
    return path.is_symlink(), target, path.read_bytes(), stat.st_mtime_ns


def test_rerun_reuses_verified_models_and_existing_comfy_without_network(tmp_path):
    fixture = _setup_rerun_fixture(tmp_path)
    env = fixture["env"]
    bundle = fixture["bundle"]
    install = fixture["install"]
    model_paths = fixture["model_paths"]
    comfy_settings = fixture["comfy_settings"]
    existing_paths = [
        fixture["comfy_main"], fixture["comfy_python"], fixture["comfy_pyvenv"],
        comfy_settings, fixture["comfy_model_paths"],
    ]
    existing_paths.extend(path for path in fixture["models"].rglob("*") if path.is_file() or path.is_symlink())
    existing_paths.append(fixture["external"])
    existing_before = {str(path): _snapshot(path) for path in existing_paths}
    log = fixture["log"]
    launcher = install / "start-comfyui-qwen21.command"

    first = run_script(bundle / "setup-qwen21-macos.command", "--turbo", env=env)
    assert first.returncode == 0, first.stderr + first.stdout
    assert "Existing ComfyUI installation found" in first.stdout
    assert launcher.is_file()
    assert (install / "ComfyUI/custom_nodes/frosty_viggle_turbo_v021/viggle_turbo.py").is_file()
    assert {str(path): _snapshot(path) for path in existing_paths} == existing_before
    model_paths_before = model_paths.read_bytes(), model_paths.stat().st_mtime_ns
    comfy_before = comfy_settings.read_bytes(), comfy_settings.stat().st_mtime_ns
    launcher_before = launcher.read_bytes(), launcher.stat().st_mtime_ns
    node = install / "ComfyUI/custom_nodes/frosty_viggle_turbo_v021"
    node_before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in node.iterdir() if path.is_file()}

    log.write_text("")
    second = run_script(bundle / "setup-qwen21-macos.command", "--turbo", env=env)
    assert second.returncode == 0, second.stderr + second.stdout
    assert "Existing Frosty ComfyUI launcher is current; skipping copy" in second.stdout
    assert "Existing Frosty Viggle Turbo node bundle is current; skipping copy" in second.stdout
    calls = log.read_text()
    assert not any(line.startswith(("curl ", "git ", "brew ")) for line in calls.splitlines())
    assert "-m pip" not in calls and "-m venv" not in calls
    assert any(line.startswith("stat follow=1 ") for line in calls.splitlines())
    assert (model_paths.read_bytes(), model_paths.stat().st_mtime_ns) == model_paths_before
    assert (comfy_settings.read_bytes(), comfy_settings.stat().st_mtime_ns) == comfy_before
    assert {str(path): _snapshot(path) for path in existing_paths} == existing_before
    assert (launcher.read_bytes(), launcher.stat().st_mtime_ns) == launcher_before
    assert {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in node.iterdir() if path.is_file()} == node_before

    external = fixture["external"]
    target = fixture["models"] / "diffusion_models/qwen-image-2.1-Q4_K_M.gguf"
    external.write_bytes(b"bad")
    bad_before = external.read_bytes(), external.stat().st_mtime_ns, target.readlink()
    log.write_text("")
    invalid = run_script(bundle / "setup-qwen21-macos.command", "--turbo", env=env)
    assert invalid.returncode != 0
    assert "Existing model failed verification" in invalid.stderr
    assert (external.read_bytes(), external.stat().st_mtime_ns, target.readlink()) == bad_before
    calls = log.read_text()
    assert not any(line.startswith(("curl ", "git ", "brew ")) for line in calls.splitlines())
