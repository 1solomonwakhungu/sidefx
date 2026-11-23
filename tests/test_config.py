"""Unit tests for `.sidefx.json` config discovery, loading, and validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sidefx.config import (
    CONFIG_FILENAME,
    DEFAULT_MUTATE_PREFIXES,
    DEFAULT_READ_PREFIXES,
    ENV_CONFIG_PATH,
    ConfigError,
    SidefxConfig,
    UnknownToolPolicy,
    default_config_dict,
    discover_config_path,
    load_config,
)


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


class TestConfigDiscoveryAndDefaults:
    def test_defaults_when_no_config_file_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
        cfg = load_config(cwd=tmp_path)
        assert cfg == SidefxConfig()
        assert cfg.source_path is None
        assert cfg.tools.unknown_tool_policy == UnknownToolPolicy.MUTATE
        assert cfg.tools.read_prefixes == DEFAULT_READ_PREFIXES
        assert cfg.tools.mutate_prefixes == DEFAULT_MUTATE_PREFIXES
        assert cfg.default_timeout_seconds == 30.0

    def test_upward_discovery_from_nested_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
        root_cfg = _write_json(
            tmp_path / CONFIG_FILENAME,
            {"version": 1, "default_actor": "ci-bot"},
        )
        nested = tmp_path / "packages" / "service" / "src"
        nested.mkdir(parents=True)

        assert discover_config_path(cwd=nested) == root_cfg.resolve()
        cfg = load_config(cwd=nested)
        assert cfg.default_actor == "ci-bot"
        assert cfg.source_path == root_cfg.resolve()

    def test_precedence_explicit_over_env_over_discovered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_json(tmp_path / CONFIG_FILENAME, {"default_actor": "discovered"})
        env_file = _write_json(tmp_path / "env.json", {"default_actor": "from-env"})
        explicit_file = _write_json(tmp_path / "explicit.json", {"default_actor": "from-explicit"})

        monkeypatch.setenv(ENV_CONFIG_PATH, str(env_file))
        assert load_config(cwd=tmp_path).default_actor == "from-env"
        assert load_config(explicit_file, cwd=tmp_path).default_actor == "from-explicit"

    def test_missing_explicit_or_env_path_raises_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "does-not-exist.json"
        with pytest.raises(ConfigError, match="not found"):
            load_config(missing)

        monkeypatch.setenv(ENV_CONFIG_PATH, str(missing))
        with pytest.raises(ConfigError, match="not found"):
            load_config(cwd=tmp_path)


class TestConfigParsingAndValidation:
    def test_default_config_dict_round_trips(self, tmp_path: Path) -> None:
        cfg_file = _write_json(tmp_path / CONFIG_FILENAME, default_config_dict())
        loaded = load_config(cfg_file)
        assert loaded.to_dict() == default_config_dict()

    def test_valid_full_config_with_rules(self, tmp_path: Path) -> None:
        cfg_file = _write_json(
            tmp_path / CONFIG_FILENAME,
            {
                "version": 1,
                "journal_path": ".sidefx/custom.db",
                "default_actor": "coder",
                "default_session_id": "sess-default",
                "default_timeout_seconds": 45,
                "tools": {
                    "read": ["inspect_repo"],
                    "mutate": ["deploy_release"],
                    "read_prefixes": ["get_", "list_"],
                    "mutate_prefixes": ["create_", "delete_"],
                    "unknown_tool_policy": "deny",
                },
                "preflight": {
                    "rules": [
                        {
                            "tool": "refund_*",
                            "arg": "amount",
                            "min": 0,
                            "max": 50,
                            "reason": "Refund cap exceeded",
                        },
                        {
                            "tool": "send_email",
                            "arg": "to",
                            "allow_domains": ["example.com"],
                            "deny_domains": ["competitor.com"],
                        },
                        {
                            "tool": "git_push",
                            "arg": "branch",
                            "deny": ["main", "master"],
                            "allow": ["feat/*", "fix/*"],
                        },
                    ]
                },
            },
        )
        cfg = load_config(cfg_file)
        assert cfg.journal_path == ".sidefx/custom.db"
        assert cfg.default_actor == "coder"
        assert cfg.default_session_id == "sess-default"
        assert cfg.default_timeout_seconds == 45.0
        assert cfg.tools.read_tools == ("inspect_repo",)
        assert cfg.tools.mutate_tools == ("deploy_release",)
        assert cfg.tools.unknown_tool_policy == UnknownToolPolicy.DENY
        assert len(cfg.preflight.rules) == 3
        assert cfg.preflight.rules[0].max_value == 50
        assert cfg.preflight.rules[0].min_value == 0
        assert cfg.preflight.rules[1].allow_domains == ("example.com",)
        assert cfg.preflight.rules[2].deny == ("main", "master")

    def test_malformed_json_and_directory_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(ConfigError, match="Malformed JSON"):
            load_config(bad)

        with pytest.raises(ConfigError, match="directory"):
            load_config(tmp_path)

    @pytest.mark.parametrize(
        ("payload", "match"),
        [
            ([], "JSON object"),
            ({"unknown_key": 1}, "Unknown key"),
            ({"version": 2}, "Unsupported config version"),
            ({"version": True}, "version must be an integer"),
            ({"default_timeout_seconds": 0}, "must be > 0"),
            ({"default_timeout_seconds": -5}, "must be > 0"),
            ({"default_timeout_seconds": True}, "finite number"),
            ({"default_actor": ""}, "non-empty string"),
            ({"tools": "bad"}, "'tools' must be a JSON object"),
            ({"tools": {"extra": []}}, "Unknown key"),
            ({"tools": {"read": ["x"], "mutate": ["x"]}}, "both tools.read and tools.mutate"),
            ({"tools": {"unknown_tool_policy": "allow_all"}}, "unknown_tool_policy"),
            ({"preflight": {"extra": []}}, "Unknown key"),
            ({"preflight": {"rules": "not-a-list"}}, "must be a list"),
            ({"preflight": {"rules": [{"tool": "*"}]}}, "arg is required"),
            ({"preflight": {"rules": [{"arg": "amount"}]}}, "at least one constraint"),
            ({"preflight": {"rules": [{"arg": "amount", "max": True}]}}, "finite number"),
            ({"preflight": {"rules": [{"arg": "amount", "min": 100, "max": 50}]}}, "min"),
            ({"preflight": {"rules": [{"arg": "branch", "alow": ["feat/*"]}]}}, "Unknown key"),
            ({"preflight": {"rules": [{"arg": "branch", "deny": [""]}]}}, "non-empty string"),
        ],
    )
    def test_invalid_schema_variants_raise_config_error(
        self, tmp_path: Path, payload: Any, match: str
    ) -> None:
        cfg_file = _write_json(tmp_path / "invalid.json", payload)
        with pytest.raises(ConfigError, match=match):
            load_config(cfg_file)
