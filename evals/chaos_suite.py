"""Standalone entry point and re-exports for the sidefx chaos fault-injection suite.

Run directly via::

    uv run python evals/chaos_suite.py
    uv run python evals/chaos_suite.py --verbose
    uv run python evals/chaos_suite.py --json

Or through the installed CLI::

    uv run sidefx eval
"""

from __future__ import annotations

from sidefx.chaos import (
    SCENARIO_REGISTRY,
    ChaosSuiteReport,
    ScenarioResult,
    available_scenario_names,
    format_chaos_report,
    handle_eval_cli,
    main,
    run_chaos_suite,
)

__all__ = [
    "SCENARIO_REGISTRY",
    "ChaosSuiteReport",
    "ScenarioResult",
    "available_scenario_names",
    "format_chaos_report",
    "handle_eval_cli",
    "main",
    "run_chaos_suite",
]


if __name__ == "__main__":
    raise SystemExit(main())
