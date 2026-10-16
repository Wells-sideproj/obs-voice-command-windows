from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import windows11_lab as lab_module
from tools.windows11_lab import (
    LabConfigurationError,
    LabSafetyError,
    TcpListener,
    assert_endpoint_free,
    assert_listener_owned,
    inspect_tcp_listeners,
    load_lab_fixture,
    local_lab_mutex,
    validate_lab_fixture,
)


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo_root = tmp_path / "checkout"
    lab_root = tmp_path / "isolated-lab"
    obs_root = lab_root / "obs" / "bin" / "64bit"
    runtime_root = lab_root / "runtime"
    for directory in (
        repo_root,
        obs_root,
        runtime_root / "project-venv",
        runtime_root / "uv-cache",
        runtime_root / "uv",
        runtime_root / "python312",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (obs_root / "obs64.exe").write_bytes(b"portable-obs-placeholder")
    (runtime_root / "uv" / "uv.exe").write_bytes(b"uv-placeholder")
    (runtime_root / "python312" / "python.exe").write_bytes(b"python-placeholder")
    (repo_root / "uv.lock").write_text("# frozen fixture lock\n", encoding="utf-8")
    fixture = tmp_path / "windows11-lab.toml"
    fixture.write_text(
        f"""schema_version = 1

[lab]
lab_root = "{lab_root.as_posix()}"

[obs]
executable = "{(obs_root / 'obs64.exe').as_posix()}"
portable = true
profile = "w11-011-certification"
collection = "w11-011-certification"
host = "127.0.0.1"
port = 4455
scene = "Test Scene"
source = "Display Capture"

[runtime]
python_path = "{(runtime_root / 'python312' / 'python.exe').as_posix()}"
uv_path = "{(runtime_root / 'uv' / 'uv.exe').as_posix()}"
uv_version = "0.11.8"
project_venv = "{(runtime_root / 'project-venv').as_posix()}"
cache_dir = "{(runtime_root / 'uv-cache').as_posix()}"
lockfile = "{(repo_root / 'uv.lock').as_posix()}"

[zoom]
level = 2.0
deadzone = 0.15
smoothing = 0.12
""",
        encoding="utf-8",
    )
    return fixture, repo_root, lab_root


def test_complete_fixture_is_positive_and_path_names_are_not_secrets(tmp_path: Path) -> None:
    fixture, repo_root, lab_root = _write_fixture(tmp_path)

    lab = validate_lab_fixture(fixture, repo_root=repo_root, require_existing=True)

    assert lab.obs.lab_root == lab_root.resolve()
    assert lab.obs.scene == "Test Scene"
    assert lab.obs.source == "Display Capture"
    assert lab.runtime.python_path.name == "python.exe"
    assert lab.runtime.uv_path.name == "uv.exe"
    assert lab.runtime.uv_version == "0.11.8"
    assert lab.zoom.level == pytest.approx(2.0)


def test_fixture_summary_is_structural_only_and_never_runtime_verified(tmp_path: Path) -> None:
    fixture, repo_root, _ = _write_fixture(tmp_path)

    lab = validate_lab_fixture(fixture, repo_root=repo_root, require_existing=True)
    summary = lab.public_summary()

    assert summary["status"] == "structural_valid"
    assert summary["structural_valid"] is True
    assert summary["runtime_verified"] is False
    assert "python_path" not in str(summary)
    assert "Test Scene" not in str(summary)


def test_structural_validation_never_executes_fixture_runtime(tmp_path: Path, monkeypatch) -> None:
    fixture, repo_root, _ = _write_fixture(tmp_path)

    def fail_run(*args, **kwargs):
        del args, kwargs
        raise AssertionError("fixture runtime must not be executed")

    monkeypatch.setattr(lab_module.subprocess, "run", fail_run)
    lab = validate_lab_fixture(fixture, repo_root=repo_root, require_existing=True)
    assert lab.runtime_verified is False


def test_missing_scene_source_or_zoom_is_rejected_before_defaults(tmp_path: Path) -> None:
    fixture, repo_root, _ = _write_fixture(tmp_path)
    content = fixture.read_text(encoding="utf-8")
    fixture.write_text(content.replace('scene = "Test Scene"\n', ""), encoding="utf-8")

    with pytest.raises(LabConfigurationError, match="scene"):
        load_lab_fixture(fixture, repo_root=repo_root)


def test_sensitive_key_detection_uses_key_boundaries(tmp_path: Path) -> None:
    fixture, repo_root, _ = _write_fixture(tmp_path)
    content = fixture.read_text(encoding="utf-8")
    fixture.write_text(
        content.replace(
            '[zoom]\nlevel = 2.0',
            '[metadata]\npython_path_alias = "allowed"\n\n[zoom]\nlevel = 2.0',
        ),
        encoding="utf-8",
    )
    assert load_lab_fixture(fixture, repo_root=repo_root).runtime.python_path.name == "python.exe"

    fixture.write_text(
        content.replace(
            '[zoom]\nlevel = 2.0',
            '[metadata]\nmanaged_pat = "must-not-be-here"\n\n[zoom]\nlevel = 2.0',
        ),
        encoding="utf-8",
    )
    with pytest.raises(LabConfigurationError, match="managed_pat"):
        load_lab_fixture(fixture, repo_root=repo_root)


def test_lab_root_inside_checkout_is_rejected(tmp_path: Path) -> None:
    fixture, repo_root, _ = _write_fixture(tmp_path)
    content = fixture.read_text(encoding="utf-8")
    fixture.write_text(
        content.replace(
            f'lab_root = "{(tmp_path / "isolated-lab").as_posix()}"',
            f'lab_root = "{repo_root.as_posix()}"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(LabConfigurationError, match="independent"):
        load_lab_fixture(fixture, repo_root=repo_root)


def test_listener_inspection_fails_closed_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(lab_module.os, "name", "posix")

    with pytest.raises(LabSafetyError, match="only supported on Windows"):
        inspect_tcp_listeners()


def test_listener_inspection_does_not_hide_powershell_errors(monkeypatch) -> None:
    monkeypatch.setattr(lab_module.os, "name", "nt")

    def fail_run(*args, **kwargs):
        del args, kwargs
        raise subprocess.CalledProcessError(1, "powershell.exe")

    monkeypatch.setattr(lab_module.subprocess, "run", fail_run)
    with pytest.raises(LabSafetyError, match="unable to inspect"):
        inspect_tcp_listeners()


def test_listener_empty_json_array_means_no_listener() -> None:
    assert lab_module._parse_listener_json("[]", 4455) == []


def test_listener_unknown_empty_output_fails_closed() -> None:
    with pytest.raises(LabSafetyError, match="no result"):
        lab_module._parse_listener_json("", 4455)


def test_lab_cli_uses_allowlisted_diagnostic_without_fixture_values(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "windows11_lab.py",
            "--fixture",
            "C:/secret/sentinel-fixture.toml",
            "--repo-root",
            "C:/secret/sentinel-checkout",
            "--require-existing",
        ],
    )

    assert lab_module._cli() == 1
    output = capsys.readouterr().out
    assert "sentinel-fixture" not in output
    assert "str(exc)" not in output
    assert '"code"' in output


def test_lab_cli_sanitizes_unexpected_path_or_parser_errors(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        lab_module,
        "validate_lab_fixture",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("path-secret\nTOKEN nested subprocess output")
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "windows11_lab.py",
            "--fixture",
            "C:/secret/sentinel-fixture.toml",
            "--repo-root",
            "C:/secret/sentinel-checkout",
        ],
    )

    assert lab_module._cli() == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == {"code": "LAB_CLI_FAILED", "stage": "lab", "status": "blocked"}
    assert "path-secret" not in captured.out
    assert "TOKEN" not in captured.out
    assert "subprocess" not in captured.out
    assert captured.err == ""


def test_lab_cli_allowlists_configuration_and_safety_codes(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "windows11_lab.py",
            "--fixture",
            "sentinel-fixture",
            "--repo-root",
            "sentinel-root",
        ],
    )

    monkeypatch.setattr(
        lab_module,
        "validate_lab_fixture",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            LabConfigurationError("fixture-secret\nTOKEN", code="SECRET_CODE")
        ),
    )
    assert lab_module._cli() == 1
    configuration_output = capsys.readouterr()
    assert json.loads(configuration_output.out)["code"] == "INVALID_FIXTURE"
    assert "fixture-secret" not in configuration_output.out
    assert "TOKEN" not in configuration_output.out
    assert configuration_output.err == ""

    monkeypatch.setattr(
        lab_module,
        "validate_lab_fixture",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            LabSafetyError("safety-secret\nTOKEN", code="SECRET_CODE")
        ),
    )
    assert lab_module._cli() == 1
    safety_output = capsys.readouterr()
    assert json.loads(safety_output.out)["code"] == "SAFETY_GUARD_FAILED"
    assert "safety-secret" not in safety_output.out
    assert "TOKEN" not in safety_output.out
    assert safety_output.err == ""


def test_lab_cli_rejects_unknown_argument_without_echo(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "windows11_lab.py",
            "--fixture",
            "fixture-secret\nTOKEN",
            "--repo-root",
            "fixture-root",
            "--unknown-sentinel-secret\nTOKEN",
        ],
    )

    assert lab_module._cli() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "code": "INVALID_ARGUMENTS",
        "stage": "cli",
        "status": "blocked",
    }
    assert "fixture-secret" not in captured.out
    assert "TOKEN" not in captured.out
    assert "unknown-sentinel" not in captured.out
    assert captured.err == ""


def test_lab_cli_rejects_missing_argument_value_without_traceback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "windows11_lab.py",
            "--fixture",
            "fixture-secret",
            "--repo-root",
        ],
    )

    assert lab_module._cli() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "code": "INVALID_ARGUMENTS",
        "stage": "cli",
        "status": "blocked",
    }
    assert "fixture-secret" not in captured.out
    assert "Traceback" not in captured.err
    assert captured.err == ""


def test_lab_cli_help_remains_available(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["windows11_lab.py", "--help"])

    with pytest.raises(SystemExit) as raised:
        lab_module._cli()

    assert raised.value.code == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out.lower()
    assert captured.err == ""


def test_endpoint_and_owner_guards_accept_only_owned_loopback_listener() -> None:
    assert_endpoint_free(
        "127.0.0.1",
        4455,
        connect=lambda address, timeout: (_ for _ in ()).throw(ConnectionRefusedError()),
        inspector=lambda port: [],
    )
    assert_listener_owned(
        "127.0.0.1",
        4455,
        1234,
        inspector=lambda port: [TcpListener("127.0.0.1", port, 1234)],
    )


def test_local_mutex_serializes_lab_runs(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    root.mkdir()
    with local_lab_mutex(root):
        with pytest.raises(LabSafetyError, match="already owns"):
            with local_lab_mutex(root):
                pass
