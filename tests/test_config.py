"""Unit tests for `.sidefx.json` config discovery, loading, and validation."""

from __future__ import annotations

import json
import os
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
from sidefx.journal import resolve_db_path


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

    def test_upward_discovery_from_nested_directory_stops_at_git_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
        # Plant a config outside the git repo
        _write_json(tmp_path / CONFIG_FILENAME, {"version": 1, "default_actor": "outside"})
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        nested = repo / "packages" / "service"
        nested.mkdir(parents=True)

        # Discovery MUST NOT escape past `.git` boundary
        assert discover_config_path(cwd=nested) is None

        # Config inside repo root is discovered from nested subdirectory
        repo_cfg = _write_json(
            repo / CONFIG_FILENAME,
            {"version": 1, "default_actor": "ci-bot"},
        )
        assert discover_config_path(cwd=nested) == Path(os.path.abspath(repo_cfg))
        cfg = load_config(cwd=nested)
        assert cfg.default_actor == "ci-bot"
        assert cfg.source_path == Path(os.path.abspath(repo_cfg))

    def test_symlinked_config_keeps_journal_anchored_to_project_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
        monkeypatch.delenv("SIDEFX_DB", raising=False)
        dotfiles = tmp_path / "dotfiles"
        shared_cfg = _write_json(
            dotfiles / "sidefx.json",
            {"version": 1, "journal_path": ".sidefx/journal.db"},
        )
        project = tmp_path / "project_a"
        project.mkdir()
        link = project / CONFIG_FILENAME
        link.symlink_to(shared_cfg)

        cfg = load_config(cwd=project)
        assert cfg.source_path == Path(os.path.abspath(link))
        assert resolve_db_path(config=cfg, cwd=project) == project / ".sidefx" / "journal.db"

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

    def test_utf8_bom_config_is_accepted(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / CONFIG_FILENAME
        cfg_file.write_bytes(b'\xef\xbb\xbf{"version": 1, "default_actor": "bom-bot"}\n')
        cfg = load_config(cfg_file)
        assert cfg.default_actor == "bom-bot"

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

    def test_malformed_duplicate_keys_huge_int_deep_nesting_and_directory_rejected(
        self, tmp_path: Path
    ) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(ConfigError, match="Malformed JSON"):
            load_config(bad)

        dup = tmp_path / "dup.json"
        dup.write_text(
            '{"version": 1, "default_actor": "a", "default_actor": "b"}', encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="Duplicate JSON key"):
            load_config(dup)

        huge_int = tmp_path / "huge400.json"
        huge_int.write_text(
            '{"preflight": {"rules": [{"arg": "amount", "max": ' + ("9" * 400) + "}]}}",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="out of finite numeric range"):
            load_config(huge_int)

        huge_5000 = tmp_path / "huge5000.json"
        huge_5000.write_text(
            '{"preflight": {"rules": [{"arg": "amount", "max": ' + ("9" * 5000) + "}]}}",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="Invalid JSON structure"):
            load_config(huge_5000)

        deep = tmp_path / "deep.json"
        deep.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
        with pytest.raises(ConfigError, match="Invalid JSON structure"):
            load_config(deep)

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
            ({"default_timeout_seconds": 100000}, "<="),
            ({"default_timeout_seconds": True}, "finite number"),
            ({"default_actor": ""}, "non-empty string"),
            ({"default_actor": " agent "}, "leading or trailing whitespace"),
            ({"tools": "bad"}, "'tools' must be a JSON object"),
            ({"tools": {"extra": []}}, "Unknown key"),
            ({"tools": {"read": ["x"], "mutate": ["x"]}}, "both tools.read and tools.mutate"),
            (
                {"tools": {"read_prefixes": ["get_"], "mutate_prefixes": ["get_"]}},
                "both tools.read_prefixes and tools.mutate_prefixes",
            ),
            ({"tools": {"unknown_tool_policy": "allow_all"}}, "unknown_tool_policy"),
            ({"preflight": {"extra": []}}, "Unknown key"),
            ({"preflight": {"rules": "not-a-list"}}, "must be a list"),
            ({"preflight": {"rules": [{"tool": "*"}]}}, "arg is required"),
            ({"preflight": {"rules": [{"arg": "amount"}]}}, "at least one constraint"),
            ({"preflight": {"rules": [{"arg": "amount", "max": True}]}}, "finite number"),
            ({"preflight": {"rules": [{"arg": "amount", "min": 100, "max": 50}]}}, "min"),
            (
                {"preflight": {"rules": [{"arg": "env", "allow": [], "deny": ["prod"]}]}},
                "must not be empty",
            ),
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
