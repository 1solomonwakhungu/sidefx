"""Unit tests for deterministic tool classification and preflight evaluation."""

from __future__ import annotations

import math

import pytest

from sidefx.config import (
    PreflightConfig,
    PreflightRule,
    SidefxConfig,
    ToolClassificationConfig,
    UnknownToolPolicy,
)
from sidefx.preflight import (
    ToolKind,
    classify_tool,
    evaluate_preflight,
    extract_domain,
)


class TestClassifyTool:
    @pytest.mark.parametrize(
        "tool_name",
        [
            "get_issue",
            "list_repositories",
            "read_file",
            "search_issues",
            "describe_table",
            "fetch_url",
            "show_diff",
            "find_files",
            "GET_USER",
            "List_Items",
        ],
    )
    def test_default_read_prefixes(self, tool_name: str) -> None:
        assert classify_tool(tool_name) == ToolKind.READ

    @pytest.mark.parametrize(
        "tool_name",
        [
            "create_issue",
            "update_pr",
            "delete_branch",
            "remove_member",
            "post_comment",
            "send_email",
            "push_branch",
            "merge_pr",
            "refund_payment",
            "charge_card",
            "write_file",
            "put_object",
            "patch_user",
            "close_issue",
            "reopen_issue",
            "comment_pr",
            "CREATE_ISSUE",
            "Delete_Repo",
        ],
    )
    def test_default_mutate_prefixes(self, tool_name: str) -> None:
        assert classify_tool(tool_name) == ToolKind.MUTATE

    def test_explicit_exact_names_override_prefixes(self) -> None:
        cfg = SidefxConfig(
            tools=ToolClassificationConfig(
                read_tools=("create_dry_run_plan",),
                mutate_tools=("get_and_charge_token",),
            )
        )
        assert classify_tool("create_dry_run_plan", cfg) == ToolKind.READ
        assert classify_tool("get_and_charge_token", cfg) == ToolKind.MUTATE

    def test_conflict_in_explicit_lists_prefers_mutate(self) -> None:
        # Programmatically constructed config bypassing JSON overlap check must fail safe.
        tools_cfg = ToolClassificationConfig(
            read_tools=("ambiguous_tool", "shared_*"),
            mutate_tools=("ambiguous_tool", "shared_*"),
        )
        assert classify_tool("ambiguous_tool", tools_cfg) == ToolKind.MUTATE
        assert classify_tool("shared_action", tools_cfg) == ToolKind.MUTATE

    def test_glob_patterns_in_explicit_lists(self) -> None:
        tools_cfg = ToolClassificationConfig(
            read_tools=("*_readonly", "stripe_inspect_*"),
            mutate_tools=("*_destructive", "github_admin_*"),
        )
        assert classify_tool("delete_readonly", tools_cfg) == ToolKind.READ
        assert classify_tool("stripe_inspect_balance", tools_cfg) == ToolKind.READ
        assert classify_tool("get_destructive", tools_cfg) == ToolKind.MUTATE
        assert classify_tool("github_admin_sync", tools_cfg) == ToolKind.MUTATE

    def test_longest_prefix_wins_and_ties_prefer_mutate(self) -> None:
        tools_cfg = ToolClassificationConfig(
            read_prefixes=("get_", "post_preview_"),
            mutate_prefixes=("get_or_create_", "post_", "sync_"),
        )
        assert classify_tool("get_user", tools_cfg) == ToolKind.READ
        assert classify_tool("get_or_create_user", tools_cfg) == ToolKind.MUTATE
        assert classify_tool("post_message", tools_cfg) == ToolKind.MUTATE
        assert classify_tool("post_preview_message", tools_cfg) == ToolKind.READ

        tie_cfg = ToolClassificationConfig(
            read_prefixes=("run_",),
            mutate_prefixes=("RUN_",),
        )
        assert classify_tool("run_job", tie_cfg) == ToolKind.MUTATE

    def test_unknown_tool_policies(self) -> None:
        assert (
            classify_tool(
                "mystery_op",
                ToolClassificationConfig(unknown_tool_policy=UnknownToolPolicy.MUTATE),
            )
            == ToolKind.MUTATE
        )
        assert (
            classify_tool(
                "mystery_op",
                ToolClassificationConfig(unknown_tool_policy=UnknownToolPolicy.READ),
            )
            == ToolKind.READ
        )
        assert (
            classify_tool(
                "mystery_op",
                ToolClassificationConfig(unknown_tool_policy=UnknownToolPolicy.DENY),
            )
            == ToolKind.DENY
        )

    @pytest.mark.parametrize("bad_name", ["", "   ", " get_issue", "get_issue ", "a\nb", "a\x00b"])
    def test_malformed_tool_names_denied(self, bad_name: str) -> None:
        assert classify_tool(bad_name) == ToolKind.DENY

    @pytest.mark.parametrize(
        ("tool_name", "expected"),
        [
            ("fetch_and_send_email", ToolKind.MUTATE),
            ("get_and_delete_all", ToolKind.MUTATE),
            ("get_or_create_user", ToolKind.MUTATE),
            ("find_and_replace", ToolKind.MUTATE),
            ("search_and_replace", ToolKind.MUTATE),
            ("fetch_then_delete", ToolKind.MUTATE),
            ("read_modify_write", ToolKind.MUTATE),
            ("read_write_file", ToolKind.MUTATE),
            ("search_replace_file", ToolKind.MUTATE),
            ("github.delete_repo", ToolKind.MUTATE),
            ("mcp__gh__delete_repo", ToolKind.MUTATE),
            ("stripe:refund_payment", ToolKind.MUTATE),
            ("search_api.delete_index", ToolKind.MUTATE),
            ("fetch_server__delete_repo", ToolKind.MUTATE),
            ("read_replica:delete_table", ToolKind.MUTATE),
            ("mcp__gh__get_issue", ToolKind.READ),
            ("github.list_repos", ToolKind.READ),
        ],
    )
    def test_compound_and_namespaced_tool_names(self, tool_name: str, expected: ToolKind) -> None:
        cfg = ToolClassificationConfig(unknown_tool_policy=UnknownToolPolicy.READ)
        assert classify_tool(tool_name, cfg) == expected


class TestExtractDomain:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("alice@example.com", "example.com"),
            ("ALICE+tag@Sub.Example.COM", "sub.example.com"),
            ("Alice Smith <alice@example.com>", "example.com"),
            ("  user@example.com.  ", "example.com"),
            ("https://api.example.com/v1/users?fields=a,b", "api.example.com"),
            ("http://Example.COM:8080/path", "example.com"),
            ("https://example.com./v1", "example.com"),
            ("https://good.com@evil.com/path", "evil.com"),
            ("https://user:pass@Sub.Evil.COM:8443/x", "sub.evil.com"),
            ("example.com", "example.com"),
            ("api.example.com:443", "api.example.com"),
            ("http://127.0.0.1:8000/api", "127.0.0.1"),
            ("http://[::1]:8000/api", "::1"),
            ("http://[::ffff:127.0.0.1]/api", "127.0.0.1"),
            ("https://medium.com/@user/post", "medium.com"),
            ("https://registry.npmjs.org/@scope/pkg", "registry.npmjs.org"),
        ],
    )
    def test_valid_extractions(self, raw: str, expected: str) -> None:
        assert extract_domain(raw) == expected

    @pytest.mark.parametrize(
        "malformed",
        [
            "",
            "   ",
            "//cdn.example.com/asset.js",
            "//good.com/@evil.com",
            "//evil.com/@good.com",
            "https:/evil.com/@good.com",
            "https:evil.com/@good.com",
            "evil.com/@good.com",
            "evil.com?@good.com",
            "evil.com#@good.com",
            "https://good.com/<a@evil.com>",
            "https://good.com/a@evil.com",
            "http://0x/",
            "user@evil.com@good.com",
            "a@good.com, b@evil.com",
            "a@good.com; b@evil.com",
            "a@good.com\nb@evil.com",
            "a@good.com\r\nb@evil.com",
            "a@good.com\x00@evil.com",
            "a@good.com <b@evil.com>",
            "<a@good.com> <b@evil.com>",
            "Alice <a@good.com> extra",
            "user@example.com:8080",
            "user@example.com/path",
            "user@example.com..",
            "https://good.com\\@evil.com/path",
            "https://a@b@evil.com/path",
            "https://example.com:notaport/path",
            "https://example.com /path",
            "http://2130706433/",
            "http://0x7f.0.0.1/",
            "http://127.1/",
            "http://2852039166/",
            "http://0x7f000001/",
            "@example.com",
            "user@",
            "user@-bad.com",
            "user@bad..com",
            "example.com:99999",
            "not a domain",
        ],
    )
    def test_malformed_or_obfuscated_inputs_return_none(self, malformed: str) -> None:
        assert extract_domain(malformed) is None


class TestEvaluatePreflightNumeric:
    def test_min_and_max_bounds(self) -> None:
        cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(
                    PreflightRule(
                        tool="refund_payment",
                        arg="amount",
                        min_value=1,
                        max_value=50,
                        reason="auto-refund cap",
                    ),
                )
            )
        )
        assert evaluate_preflight("refund_payment", {"amount": 1}, cfg).allowed is True
        assert evaluate_preflight("refund_payment", {"amount": 50}, cfg).allowed is True
        assert evaluate_preflight("refund_payment", {"amount": 25.5}, cfg).allowed is True
        # Missing argument does not fail
        assert evaluate_preflight("refund_payment", {"currency": "USD"}, cfg).allowed is True

        too_high = evaluate_preflight("refund_payment", {"amount": 50.01}, cfg)
        assert too_high.allowed is False
        assert too_high.violations[0].constraint == "max"
        assert "auto-refund cap" in too_high.violations[0].message

        too_low = evaluate_preflight("refund_payment", {"amount": 0}, cfg)
        assert too_low.allowed is False
        assert too_low.violations[0].constraint == "min"

    @pytest.mark.parametrize(
        "bad_val",
        [True, False, float("nan"), float("inf"), float("-inf"), "25", "100", None, {"v": 1}],
    )
    def test_rejects_booleans_non_finite_and_non_numeric(self, bad_val: object) -> None:
        cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(PreflightRule(tool="refund_payment", arg="amount", max_value=50),)
            )
        )
        res = evaluate_preflight("refund_payment", {"amount": bad_val}, cfg)
        assert res.allowed is False
        assert res.violations[0].constraint == "invalid_type"

    def test_nested_dotted_paths_and_lists(self) -> None:
        cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(
                    PreflightRule(tool="create_payout", arg="payment.amount", max_value=100),
                    PreflightRule(tool="create_payout", arg="lines.amount", max_value=50),
                )
            )
        )
        ok = evaluate_preflight(
            "create_payout",
            {
                "payment": {"amount": 80},
                "lines": [{"amount": 20}, {"amount": 50}],
            },
            cfg,
        )
        assert ok.allowed is True

        bad_nested = evaluate_preflight(
            "create_payout",
            {
                "payment": {"amount": 120},
                "lines": [{"amount": 10}, {"amount": 75}],
            },
            cfg,
        )
        assert bad_nested.allowed is False
        assert len(bad_nested.violations) == 2
        assert {v.arg for v in bad_nested.violations} == {"payment.amount", "lines[1].amount"}

        # Flat literal dotted key cannot bypass nested rule either
        bad_flat = evaluate_preflight("create_payout", {"payment.amount": 999}, cfg)
        assert bad_flat.allowed is False
        assert bad_flat.violations[0].arg == "payment.amount"

        # Non-object intermediate (e.g. JSON-encoded string) must fail closed
        bad_intermediate = evaluate_preflight("create_payout", {"payment": '{"amount": 9999}'}, cfg)
        assert bad_intermediate.allowed is False
        assert bad_intermediate.violations[0].constraint == "invalid_type"

    def test_list_of_numbers_validates_every_item(self) -> None:
        cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(PreflightRule(tool="batch_charge", arg="amounts", max_value=50),)
            )
        )
        assert evaluate_preflight("batch_charge", {"amounts": [10, 25, 50]}, cfg).allowed is True
        res = evaluate_preflight("batch_charge", {"amounts": [10, 51, True, math.nan]}, cfg)
        assert res.allowed is False
        assert len(res.violations) == 3


class TestEvaluatePreflightStringsAndDomains:
    def test_string_allow_and_deny_with_globs_and_whitespace(self) -> None:
        cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(
                    PreflightRule(
                        tool="push_branch",
                        arg="branch",
                        allow=("feat/*", "fix/*"),
                        deny=("main", "master", "feat/protected*"),
                    ),
                    PreflightRule(
                        tool="*",
                        arg="repo",
                        allow=("myorg/*",),
                    ),
                )
            )
        )
        assert (
            evaluate_preflight(
                "push_branch", {"branch": "feat/login", "repo": "myorg/app"}, cfg
            ).allowed
            is True
        )
        # Deny wins even when allow pattern also matches
        denied_protected = evaluate_preflight(
            "push_branch", {"branch": "feat/protected-release", "repo": "myorg/app"}, cfg
        )
        assert denied_protected.allowed is False
        assert denied_protected.violations[0].constraint == "deny"

        # Whitespace padded denied branch still caught by deny
        padded_main = evaluate_preflight("push_branch", {"branch": " main "}, cfg)
        assert padded_main.allowed is False
        assert any(v.constraint == "deny" for v in padded_main.violations)

        # Whitespace padded allowed value rejected by allow
        padded_allow = evaluate_preflight("push_branch", {"branch": " feat/login "}, cfg)
        assert padded_allow.allowed is False
        assert any(v.constraint == "allow" for v in padded_allow.violations)

        # Non-string value fails closed
        non_str = evaluate_preflight("push_branch", {"branch": ["feat/ok", 123]}, cfg)
        assert non_str.allowed is False
        assert non_str.violations[0].constraint == "invalid_type"

    def test_domain_allowlist_and_subdomain_boundaries(self) -> None:
        cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(
                    PreflightRule(
                        tool="send_email",
                        arg="recipients",
                        allow_domains=("company.com", "*.partner.org"),
                    ),
                )
            )
        )
        ok = evaluate_preflight(
            "send_email",
            {
                "recipients": [
                    "alice@company.com",
                    "Bob <bob@COMPANY.COM>",
                    "ops@mail.partner.org",
                ]
            },
            cfg,
        )
        assert ok.allowed is True

        # Exact domain rule must NOT silently allow subdomains or suffix spoofs
        for bad_target in (
            "attacker@sub.company.com",
            "attacker@evil-company.com",
            "attacker@company.com.evil.com",
            "attacker@partner.org",
            "attacker@evilpartner.org",
            "user@evil.com@company.com",
            "good@company.com, evil@attacker.com",
            "good@company.com\nevil@attacker.com",
        ):
            res = evaluate_preflight(
                "send_email", {"recipients": ["ok@company.com", bad_target]}, cfg
            )
            assert res.allowed is False, f"Expected denial for {bad_target!r}"

    def test_domain_denylist_blocks_subdomains_and_url_userinfo_spoof(self) -> None:
        cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(
                    PreflightRule(
                        tool="post_webhook",
                        arg="url",
                        deny_domains=("evil.com", "169.254.169.254"),
                    ),
                )
            )
        )
        assert (
            evaluate_preflight("post_webhook", {"url": "https://api.notevil.com/hook"}, cfg).allowed
            is True
        )
        for bad_url in (
            "https://evil.com/hook",
            "https://api.evil.com:8443/hook",
            "https://good.com@evil.com/hook",
            "https://good.com\\@evil.com/hook",
            "http://169.254.169.254/latest/meta-data/",
        ):
            res = evaluate_preflight("post_webhook", {"url": bad_url}, cfg)
            assert res.allowed is False, f"Expected denial for {bad_url!r}"

    def test_unknown_tool_policy_deny_and_non_mapping_arguments(self) -> None:
        cfg = SidefxConfig(
            tools=ToolClassificationConfig(unknown_tool_policy=UnknownToolPolicy.DENY)
        )
        denied = evaluate_preflight("mystery_tool", {}, cfg)
        assert denied.allowed is False
        assert denied.tool_kind == ToolKind.DENY
        assert denied.violations[0].constraint == "tool_denied"

        bad_args = evaluate_preflight("create_issue", ["not", "a", "dict"])  # type: ignore[arg-type]
        assert bad_args.allowed is False
        assert bad_args.violations[0].constraint == "invalid_type"

    def test_url_email_ambiguity_alternate_ips_invalid_patterns_and_path_traversal(self) -> None:
        # B2: URL-versus-email ambiguity blocked under both allow_domains and deny_domains
        allow_cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(PreflightRule(tool="send_email", arg="to", allow_domains=("good.com",)),)
            )
        )
        deny_cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(PreflightRule(tool="send_email", arg="to", deny_domains=("evil.com",)),)
            )
        )
        for ambiguous in (
            "//good.com/@evil.com",
            "//evil.com/@good.com",
            "https:/evil.com/@good.com",
            "https:evil.com/@good.com",
            "evil.com/@good.com",
            "evil.com?@good.com",
            "evil.com#@good.com",
            "https://good.com/<a@evil.com>",
            "https://good.com/a@evil.com",
        ):
            assert evaluate_preflight("send_email", {"to": ambiguous}, allow_cfg).allowed is False
            assert evaluate_preflight("send_email", {"to": ambiguous}, deny_cfg).allowed is False

        # Ordinary @handle URLs must NOT be over-blocked under allow_domains
        handle_cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(
                    PreflightRule(
                        tool="fetch_url",
                        arg="url",
                        allow_domains=("medium.com", "registry.npmjs.org"),
                    ),
                )
            )
        )
        assert (
            evaluate_preflight(
                "fetch_url", {"url": "https://medium.com/@user/post"}, handle_cfg
            ).allowed
            is True
        )
        assert (
            evaluate_preflight(
                "fetch_url", {"url": "https://registry.npmjs.org/@scope/pkg"}, handle_cfg
            ).allowed
            is True
        )

        # N4: alternate IP encodings blocked against deny_domains
        ip_deny_cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(
                    PreflightRule(
                        tool="fetch_url",
                        arg="url",
                        deny_domains=("127.0.0.1", "169.254.169.254"),
                    ),
                )
            )
        )
        for alt_ip_url in (
            "http://2130706433/",
            "http://0x7f.0.0.1/",
            "http://127.1/",
            "http://[::ffff:127.0.0.1]/",
            "http://2852039166/",
        ):
            assert (
                evaluate_preflight("fetch_url", {"url": alt_ip_url}, ip_deny_cfg).allowed is False
            )

        # N5: malformed deny_domains patterns fail closed instead of silently matching nothing
        for bad_pat in ("evil.*", "*evil.com", "https://evil.com", "evil.com:8080"):
            bad_rule_cfg = SidefxConfig(
                preflight=PreflightConfig(
                    rules=(PreflightRule(tool="post_url", arg="url", deny_domains=(bad_pat,)),)
                )
            )
            res = evaluate_preflight("post_url", {"url": "https://example.com"}, bad_rule_cfg)
            assert res.allowed is False
            assert res.violations[0].constraint == "invalid_rule"

        # N7: case-insensitive deny + double-slash + path traversal blocked
        str_cfg = SidefxConfig(
            preflight=PreflightConfig(
                rules=(
                    PreflightRule(tool="push_branch", arg="repo", deny=("myorg/prod",)),
                    PreflightRule(
                        tool="write_file", arg="path", allow=("/workspace/*",), deny=("/etc/*",)
                    ),
                )
            )
        )
        assert evaluate_preflight("push_branch", {"repo": "MyOrg/Prod"}, str_cfg).allowed is False
        assert (
            evaluate_preflight("write_file", {"path": "/workspace/../etc/passwd"}, str_cfg).allowed
            is False
        )
        assert evaluate_preflight("write_file", {"path": "//etc/passwd"}, str_cfg).allowed is False
        assert (
            evaluate_preflight("write_file", {"path": "/workspace/app.py"}, str_cfg).allowed is True
        )
