"""Pytest integration for the chaos evaluation suite and end-to-end CLI smoke tests."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from sidefx import __version__
from sidefx.chaos import (
    ChaosSuiteReport,
    ScenarioResult,
    available_scenario_names,
    format_chaos_report,
    run_chaos_suite,
)
from sidefx.config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent


def _sidefx_cmd(*args: str) -> list[str]:
    """Invoke the installed `sidefx` console script in the active venv (or fallback to -m)."""
    candidate = Path(sys.executable).resolve().parent / "sidefx"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return [str(candidate), *args]
    which = shutil.which("sidefx")
    if which:
        return [which, *args]
    return [sys.executable, "-m", "sidefx", *args]


class TestChaosSuite:
    def test_full_chaos_suite_zero_duplicates_and_100pct_receipt_accuracy(self) -> None:
        tmp_root = Path(tempfile.gettempdir())
        before_dirs = {p.name for p in tmp_root.glob("sidefx-chaos-*")}

        report = run_chaos_suite()

        after_dirs = {p.name for p in tmp_root.glob("sidefx-chaos-*")}
        assert after_dirs == before_dirs, f"Leaked temp directories: {after_dirs - before_dirs}"

        assert report.passed is True, format_chaos_report(report, verbose=True)
        assert report.total_scenarios == 6
        assert report.passed_scenarios == 6
        assert report.failed_scenarios == 0
        assert report.duplicate_remote_writes == 0
        assert report.receipt_accuracy_pct == 100.0
        assert report.total_duration_ms > 0

        names = {sc.name for sc in report.scenarios}
        assert names == set(available_scenario_names())
        for sc in report.scenarios:
            assert sc.passed is True, f"Scenario {sc.name} failed: {sc.error} / {sc.subchecks}"
            assert sc.duplicate_remote_writes == 0
            assert sc.actual_remote_writes == sc.expected_remote_writes
            assert sc.subchecks

        payload = report.to_dict()
        assert payload["passed"] is True
        assert payload["duplicate_remote_writes"] == 0
        assert payload["receipt_accuracy_pct"] == 100.0
        assert len(payload["scenarios"]) == 6

        rendered = format_chaos_report(report, verbose=True)
        assert "Result: PASS" in rendered
        assert "Duplicate remote writes: 0" in rendered
        assert "Receipt accuracy: 100.0%" in rendered

    def test_evals_chaos_suite_module_and_script_entry_point(self) -> None:
        script_path = REPO_ROOT / "evals" / "chaos_suite.py"
        assert script_path.is_file()

        spec = importlib.util.spec_from_file_location("chaos_suite", script_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.run_chaos_suite is run_chaos_suite

        proc = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--scenario",
                "preflight_policy_enforcement",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        assert data["passed"] is True
        assert data["total_scenarios"] == 1
        assert data["duplicate_remote_writes"] == 0
        assert data["receipt_accuracy_pct"] is None

    def test_unknown_scenario_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown chaos scenario"):
            run_chaos_suite(scenarios=["nonexistent_scenario"])

    def test_report_fails_closed_on_duplicates_or_inaccurate_receipt(self) -> None:
        dup_report = ChaosSuiteReport(
            scenarios=(
                ScenarioResult(
                    name="dup_check",
                    title="Dup",
                    passed=True,
                    duration_ms=1.0,
                    duplicate_remote_writes=1,
                    expected_remote_writes=1,
                    actual_remote_writes=2,
                ),
            ),
            total_duration_ms=1.0,
        )
        assert dup_report.passed is False
        assert dup_report.duplicate_remote_writes == 1

        bad_receipt_report = ChaosSuiteReport(
            scenarios=(
                ScenarioResult(
                    name="receipt_check",
                    title="Receipt",
                    passed=True,
                    duration_ms=1.0,
                    duplicate_remote_writes=0,
                    expected_remote_writes=2,
                    actual_remote_writes=2,
                    receipt_accuracy_pct=95.0,
                ),
            ),
            total_duration_ms=1.0,
        )
        assert bad_receipt_report.passed is False
        assert bad_receipt_report.receipt_accuracy_pct == 95.0


class TestCLISmokeEndToEnd:
    def test_version_and_help(self) -> None:
        v_proc = subprocess.run(
            _sidefx_cmd("--version"),
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        assert v_proc.returncode == 0
        assert f"sidefx {__version__}" in v_proc.stdout

        h_proc = subprocess.run(
            _sidefx_cmd("--help"),
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        assert h_proc.returncode == 0
        for subcmd in ("status", "init", "report", "reconcile", "wrap", "mcp-proxy", "eval"):
            assert subcmd in h_proc.stdout

    def test_init_and_claude_code_hook_smoke(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / ".sidefx.json"
        init_proc = subprocess.run(
            _sidefx_cmd("init", "--path", str(cfg_file)),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        assert init_proc.returncode == 0, init_proc.stderr
        assert cfg_file.is_file()
        loaded = load_config(cfg_file)
        assert loaded.version == 1

        snippet_proc = subprocess.run(
            _sidefx_cmd("init", "--claude-code"),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        assert snippet_proc.returncode == 0, snippet_proc.stderr
        snippet = json.loads(snippet_proc.stdout)
        assert (
            snippet["hooks"]["Stop"][0]["hooks"][0]["command"] == "sidefx report --format markdown"
        )

        settings_file = tmp_path / ".claude" / "settings.json"
        merge_proc = subprocess.run(
            _sidefx_cmd("init", "--claude-code", "--settings", str(settings_file)),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        assert merge_proc.returncode == 0, merge_proc.stderr
        assert settings_file.is_file()
        merged = json.loads(settings_file.read_text(encoding="utf-8"))
        assert len(merged["hooks"]["Stop"]) == 1

    def test_wrap_status_reconcile_report_smoke(self, tmp_path: Path) -> None:
        db_path = tmp_path / "smoke.db"
        marker_file = tmp_path / "exec_count.txt"
        cmd_code = (
            "from pathlib import Path; "
            f"p = Path({str(marker_file)!r}); "
            "n = int(p.read_text()) if p.exists() else 0; "
            "p.write_text(str(n + 1)); "
            "print('https://example.test/smoke/1')"
        )

        # Status on missing DB succeeds cleanly
        st0 = subprocess.run(
            _sidefx_cmd("status", "--db", str(db_path), "--json"),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        assert st0.returncode == 0
        assert json.loads(st0.stdout)["total"] == 0

        # First wrap executes once
        w1 = subprocess.run(
            _sidefx_cmd(
                "wrap",
                "--db",
                str(db_path),
                "--session-id",
                "smoke-sess",
                "--action-id",
                "smoke-act-1",
                "--target",
                "smoke:target",
                "--",
                sys.executable,
                "-c",
                cmd_code,
            ),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        assert w1.returncode == 0, w1.stderr
        assert "https://example.test/smoke/1" in w1.stdout
        assert marker_file.read_text(encoding="utf-8") == "1"

        # Second wrap replays without re-executing
        w2 = subprocess.run(
            _sidefx_cmd(
                "wrap",
                "--db",
                str(db_path),
                "--session-id",
                "smoke-sess",
                "--action-id",
                "smoke-act-1",
                "--target",
                "smoke:target",
                "--",
                sys.executable,
                "-c",
                cmd_code,
            ),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        assert w2.returncode == 0, w2.stderr
        assert "https://example.test/smoke/1" in w2.stdout
        assert "replayed" in w2.stderr.lower()
        assert marker_file.read_text(encoding="utf-8") == "1"

        # Status reflects confirmed row
        st1 = subprocess.run(
            _sidefx_cmd("status", "--db", str(db_path), "--session-id", "smoke-sess", "--json"),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        assert st1.returncode == 0
        st1_data = json.loads(st1.stdout)
        assert st1_data["total"] == 1
        assert st1_data["counts"]["confirmed"] == 1

        # Reconcile succeeds with 0 unresolved actions
        rec_proc = subprocess.run(
            _sidefx_cmd("reconcile", "--db", str(db_path), "--session-id", "smoke-sess", "--json"),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        assert rec_proc.returncode == 0
        assert json.loads(rec_proc.stdout)["total"] == 0

        # Report works in markdown, text, and json formats
        for fmt in ("markdown", "text", "json"):
            rep = subprocess.run(
                _sidefx_cmd(
                    "report",
                    "--db",
                    str(db_path),
                    "--session-id",
                    "smoke-sess",
                    "--format",
                    fmt,
                ),
                cwd=str(tmp_path),
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )
            assert rep.returncode == 0, rep.stderr
            assert "smoke-act-1" in rep.stdout
            assert "https://example.test/smoke/1" in rep.stdout
            if fmt == "json":
                rep_json: dict[str, Any] = json.loads(rep.stdout)
                assert rep_json["counts"]["confirmed"] == 1

    def test_mcp_proxy_cli_smoke(self, tmp_path: Path) -> None:
        db_path = tmp_path / "mcp_smoke.db"
        server_py = tmp_path / "tiny_mcp.py"
        server_py.write_text(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    if not line.strip(): continue\n"
            "    msg = json.loads(line)\n"
            "    m = msg.get('method')\n"
            "    if m == 'initialize':\n"
            "        res = {'protocolVersion': '2024-11-05', 'serverInfo': {'name': 'tiny'}}\n"
            "    elif m == 'tools/call':\n"
            "        name = msg['params']['name']\n"
            "        res = {'content': [{'type': 'text', 'text': f'{name}: https://ok.test/1'}], "
            "'isError': False}\n"
            "    else:\n"
            "        res = {'ok': True}\n"
            "    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': res})"
            " + '\\n')\n"
            "    sys.stdout.flush()\n",
            encoding="utf-8",
        )

        requests = (
            "\n".join(
                [
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "get_item", "arguments": {"id": 1}},
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/call",
                            "params": {
                                "name": "create_item",
                                "arguments": {"_sidefx_action_id": "mcp-smoke-1", "title": "hi"},
                            },
                        }
                    ),
                ]
            )
            + "\n"
        )

        proc = subprocess.run(
            _sidefx_cmd(
                "mcp-proxy",
                "--db",
                str(db_path),
                "--session-id",
                "mcp-smoke",
                "--",
                sys.executable,
                str(server_py),
            ),
            input=requests,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        assert len(replies) == 3
        assert replies[2]["result"]["isError"] is False

        rep = subprocess.run(
            _sidefx_cmd("report", "--db", str(db_path), "--session-id", "mcp-smoke", "--json"),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        assert rep.returncode == 0
        rep_data = json.loads(rep.stdout)
        assert rep_data["total"] == 1
        assert rep_data["groups"]["confirmed"][0]["action_id"] == "mcp-smoke-1"
        assert rep_data["groups"]["confirmed"][0]["result_ref"] == "https://ok.test/1"

    def test_eval_cli_smoke_and_isolated_from_broken_cwd_config(self, tmp_path: Path) -> None:
        # Plant an intentionally malformed .sidefx.json in cwd to verify `sidefx eval`
        # stays completely isolated from ambient repository configs.
        (tmp_path / ".sidefx.json").write_text("{not valid json", encoding="utf-8")

        help_proc = subprocess.run(
            _sidefx_cmd("eval", "--help"),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        assert help_proc.returncode == 0
        assert "--json" in help_proc.stdout
        assert "--verbose" in help_proc.stdout

        json_proc = subprocess.run(
            _sidefx_cmd("eval", "--scenario", "payload_mutation_reused_id", "--json"),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
        )
        assert json_proc.returncode == 0, json_proc.stderr
        data = json.loads(json_proc.stdout)
        assert data["passed"] is True
        assert data["duplicate_remote_writes"] == 0

        text_proc = subprocess.run(
            _sidefx_cmd("eval", "--scenario", "payload_mutation_reused_id", "--verbose"),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
        )
        assert text_proc.returncode == 0, text_proc.stderr
        assert "PASS" in text_proc.stdout
        assert "Duplicate remote writes: 0" in text_proc.stdout
