"""Reproducible latency and overhead benchmarks for sidefx.

Run directly via::

    uv run python evals/benchmark.py
    uv run python evals/benchmark.py --json
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from sidefx import __version__
from sidefx.journal import Journal, Surface

_BENCH_MCP_SERVER = """\
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    req_id = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"protocolVersion": "2024-11-05", "serverInfo": {"name": "bench-mcp"}},
        }
    elif method == "tools/call":
        name = (msg.get("params") or {}).get("name", "")
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": f"ok:{name}"}], "isError": False},
        }
    else:
        continue
    sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\\n")
    sys.stdout.flush()
"""


def _summarize_ms(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    n = len(ordered)
    p95_idx = min(n - 1, max(0, int(0.95 * (n - 1))))
    return {
        "n": n,
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_idx], 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
    }


def _bench_journal(root: Path, *, write_iters: int = 100, read_iters: int = 300) -> dict[str, Any]:
    db_path = root / "bench_journal.db"
    write_cycle_ms: list[float] = []
    replay_ms: list[float] = []
    lookup_ms: list[float] = []

    with Journal(db_path) as journal:
        # Warm up WAL & schema
        journal.begin_action(
            action_id="warmup",
            session_id="bench",
            actor="agent",
            surface=Surface.MCP,
            tool_name="create_item",
            target="bench",
            payload={"i": -1},
        )
        journal.mark_confirmed("agent", "bench", "warmup", result_ref="ref-warmup")

        for i in range(write_iters):
            aid = f"w-{i}"
            payload = {"i": i, "kind": "bench"}
            t0 = time.perf_counter()
            journal.begin_action(
                action_id=aid,
                session_id="bench",
                actor="agent",
                surface=Surface.MCP,
                tool_name="create_item",
                target="bench",
                payload=payload,
            )
            journal.mark_confirmed("agent", "bench", aid, result_ref=f"ref-{i}")
            write_cycle_ms.append((time.perf_counter() - t0) * 1000.0)

        for i in range(read_iters):
            idx = i % write_iters
            t0 = time.perf_counter()
            journal.begin_action(
                action_id=f"w-{idx}",
                session_id="bench",
                actor="agent",
                surface=Surface.MCP,
                tool_name="create_item",
                target="bench",
                payload={"i": idx, "kind": "bench"},
            )
            replay_ms.append((time.perf_counter() - t0) * 1000.0)

        for i in range(read_iters):
            idx = i % write_iters
            t0 = time.perf_counter()
            rec = journal.get_action("agent", "bench", f"w-{idx}")
            assert rec is not None
            lookup_ms.append((time.perf_counter() - t0) * 1000.0)

    return {
        "begin_and_confirm_write_cycle": _summarize_ms(write_cycle_ms),
        "begin_action_replay_hit": _summarize_ms(replay_ms),
        "get_action_lookup": _summarize_ms(lookup_ms),
    }


def _bench_cli_wrap(root: Path, *, iters: int = 20) -> dict[str, Any]:
    true_bin = shutil.which("true") or "/usr/bin/true"
    sidefx_bin = shutil.which("sidefx")
    sidefx_cmd = [sidefx_bin] if sidefx_bin else [sys.executable, "-m", "sidefx"]
    db_path = root / "wrap_bench.db"

    bare_ms: list[float] = []
    wrap_new_ms: list[float] = []
    wrap_replay_ms: list[float] = []

    # Warm up
    subprocess.run([true_bin], check=True)
    subprocess.run(
        [*sidefx_cmd, "wrap", "--db", str(db_path), "--action-id", "warm", "--", true_bin],
        capture_output=True,
        check=True,
    )

    for _ in range(iters):
        t0 = time.perf_counter()
        subprocess.run([true_bin], check=True)
        bare_ms.append((time.perf_counter() - t0) * 1000.0)

    for i in range(iters):
        t0 = time.perf_counter()
        subprocess.run(
            [
                *sidefx_cmd,
                "wrap",
                "--db",
                str(db_path),
                "--session-id",
                "bench",
                "--action-id",
                f"wrap-{i}",
                "--",
                true_bin,
            ],
            capture_output=True,
            check=True,
        )
        wrap_new_ms.append((time.perf_counter() - t0) * 1000.0)

    for i in range(iters):
        t0 = time.perf_counter()
        subprocess.run(
            [
                *sidefx_cmd,
                "wrap",
                "--db",
                str(db_path),
                "--session-id",
                "bench",
                "--action-id",
                f"wrap-{i}",
                "--",
                true_bin,
            ],
            capture_output=True,
            check=True,
        )
        wrap_replay_ms.append((time.perf_counter() - t0) * 1000.0)

    bare_sum = _summarize_ms(bare_ms)
    new_sum = _summarize_ms(wrap_new_ms)
    replay_sum = _summarize_ms(wrap_replay_ms)
    return {
        "command": true_bin,
        "bare_command": bare_sum,
        "sidefx_wrap_new": new_sum,
        "sidefx_wrap_replay": replay_sum,
        "wrap_new_overhead_median_ms": round(new_sum["median_ms"] - bare_sum["median_ms"], 3),
    }


def _rpc_roundtrip(proc: subprocess.Popen[bytes], payload: dict[str, Any]) -> float:
    assert proc.stdin is not None and proc.stdout is not None
    raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode("ascii")
    t0 = time.perf_counter()
    proc.stdin.write(raw)
    proc.stdin.flush()
    line = proc.stdout.readline()
    elapsed = (time.perf_counter() - t0) * 1000.0
    parsed = json.loads(line.decode("utf-8"))
    assert "result" in parsed, f"unexpected RPC response: {parsed}"
    return elapsed


def _bench_mcp_proxy(root: Path, *, iters: int = 80) -> dict[str, Any]:
    server_script = root / "bench_mcp_server.py"
    server_script.write_text(_BENCH_MCP_SERVER, encoding="utf-8")
    db_path = root / "mcp_bench.db"

    direct_read_ms: list[float] = []
    proxy_read_ms: list[float] = []
    proxy_mutate_ms: list[float] = []
    proxy_replay_ms: list[float] = []

    # 1. Direct stdio MCP server baseline
    direct = subprocess.Popen(
        [sys.executable, str(server_script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _rpc_roundtrip(direct, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        for i in range(iters):
            direct_read_ms.append(
                _rpc_roundtrip(
                    direct,
                    {
                        "jsonrpc": "2.0",
                        "id": i + 1,
                        "method": "tools/call",
                        "params": {"name": "get_item", "arguments": {"id": i}},
                    },
                )
            )
    finally:
        if direct.stdin:
            direct.stdin.close()
        direct.wait(timeout=5.0)

    # 2. Through `sidefx mcp-proxy --deterministic-ids`
    sidefx_bin = shutil.which("sidefx")
    proxy_cmd = [
        *([sidefx_bin] if sidefx_bin else [sys.executable, "-m", "sidefx"]),
        "mcp-proxy",
        "--db",
        str(db_path),
        "--session-id",
        "bench-mcp",
        "--target",
        "mcp:bench",
        "--deterministic-ids",
        "--",
        sys.executable,
        str(server_script),
    ]
    proxy = subprocess.Popen(
        proxy_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _rpc_roundtrip(proxy, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        # Warmup one mutate
        _rpc_roundtrip(
            proxy,
            {
                "jsonrpc": "2.0",
                "id": "warm",
                "method": "tools/call",
                "params": {"name": "create_item", "arguments": {"n": -1}},
            },
        )
        for i in range(iters):
            proxy_read_ms.append(
                _rpc_roundtrip(
                    proxy,
                    {
                        "jsonrpc": "2.0",
                        "id": f"r-{i}",
                        "method": "tools/call",
                        "params": {"name": "get_item", "arguments": {"id": i}},
                    },
                )
            )
        for i in range(iters):
            proxy_mutate_ms.append(
                _rpc_roundtrip(
                    proxy,
                    {
                        "jsonrpc": "2.0",
                        "id": f"m-{i}",
                        "method": "tools/call",
                        "params": {"name": "create_item", "arguments": {"n": i}},
                    },
                )
            )
        for i in range(iters):
            proxy_replay_ms.append(
                _rpc_roundtrip(
                    proxy,
                    {
                        "jsonrpc": "2.0",
                        "id": f"dup-{i}",
                        "method": "tools/call",
                        "params": {"name": "create_item", "arguments": {"n": i}},
                    },
                )
            )
    finally:
        if proxy.stdin:
            proxy.stdin.close()
        proxy.wait(timeout=5.0)

    direct_sum = _summarize_ms(direct_read_ms)
    pread_sum = _summarize_ms(proxy_read_ms)
    pmut_sum = _summarize_ms(proxy_mutate_ms)
    prep_sum = _summarize_ms(proxy_replay_ms)
    return {
        "direct_stdio_call": direct_sum,
        "proxy_read_passthrough": pread_sum,
        "proxy_read_overhead_median_ms": round(pread_sum["median_ms"] - direct_sum["median_ms"], 3),
        "proxy_mutate_write_ahead": pmut_sum,
        "proxy_mutate_overhead_median_ms": round(
            pmut_sum["median_ms"] - direct_sum["median_ms"], 3
        ),
        "proxy_duplicate_replay": prep_sum,
    }


def run_benchmarks() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="sidefx-bench-") as tmp:
        root = Path(tmp)
        return {
            "sidefx_version": __version__,
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "platform": f"{platform.system()} {platform.machine()}",
            "journal": _bench_journal(root),
            "cli_wrap": _bench_cli_wrap(root),
            "mcp_proxy": _bench_mcp_proxy(root),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run reproducible sidefx overhead benchmarks.")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Emit JSON output.")
    args = parser.parse_args(argv)

    results = run_benchmarks()
    if args.as_json:
        print(json.dumps(results, indent=2))
        return 0

    j = results["journal"]
    w = results["cli_wrap"]
    m = results["mcp_proxy"]
    print(
        f"sidefx {results['sidefx_version']} benchmarks "
        f"(Python {results['python_version']}, SQLite {results['sqlite_version']}, "
        f"{results['platform']})"
    )
    print("-" * 74)
    rows = [
        ("Journal: get_action lookup", j["get_action_lookup"]),
        ("Journal: begin_action replay hit", j["begin_action_replay_hit"]),
        ("Journal: begin + confirm (WAL FULL)", j["begin_and_confirm_write_cycle"]),
        ("MCP: direct stdio baseline", m["direct_stdio_call"]),
        ("MCP: proxy read-only passthrough", m["proxy_read_passthrough"]),
        ("MCP: proxy mutating write-ahead", m["proxy_mutate_write_ahead"]),
        ("MCP: proxy duplicate replay", m["proxy_duplicate_replay"]),
        ("CLI: bare `true` baseline", w["bare_command"]),
        ("CLI: `sidefx wrap` (new action)", w["sidefx_wrap_new"]),
        ("CLI: `sidefx wrap` (replayed hit)", w["sidefx_wrap_replay"]),
    ]
    print(f"{'Benchmark':<38} {'N':>5} {'Median':>10} {'p95':>10}")
    print("-" * 74)
    for label, stats in rows:
        print(f"{label:<38} {stats['n']:>5} {stats['median_ms']:>8.2f}ms {stats['p95_ms']:>8.2f}ms")
    print("-" * 74)
    print(
        f"Derived median overhead: "
        f"MCP read +{m['proxy_read_overhead_median_ms']:.2f}ms | "
        f"MCP mutate +{m['proxy_mutate_overhead_median_ms']:.2f}ms | "
        f"CLI wrap +{w['wrap_new_overhead_median_ms']:.2f}ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
