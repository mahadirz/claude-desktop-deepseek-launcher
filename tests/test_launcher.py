import json
import tempfile
import unittest
from pathlib import Path

from claude_deepseek_launcher.__main__ import (
    LaunchError,
    apply_cowork_auto_mode_config,
    apply_profile,
    estimate_input_tokens,
    normalize_api_key,
    parse_model_maps,
    rewrite_model_routes,
    rewrite_model_routes_with_map,
    sanitize_user_id_value,
    sanitize_user_ids,
    validate_api_key,
)


class LauncherHelpersTest(unittest.TestCase):
    def test_normalize_api_key_strips_bearer_prefix(self) -> None:
        self.assertEqual(normalize_api_key("Bearer sk-test"), "sk-test")
        self.assertEqual(normalize_api_key("bearer sk-test"), "sk-test")
        self.assertEqual(normalize_api_key("sk-test"), "sk-test")

    def test_validate_api_key_rejects_masked_or_wrong_key(self) -> None:
        self.assertEqual(validate_api_key("Bearer sk-test"), "sk-test")
        with self.assertRaises(LaunchError):
            validate_api_key("****-...")
        with self.assertRaises(LaunchError):
            validate_api_key("not-a-deepseek-key")

    def test_sanitize_user_id_value_matches_deepseek_pattern(self) -> None:
        self.assertEqual(sanitize_user_id_value("abc-DEF_123"), "abc-DEF_123")
        self.assertEqual(sanitize_user_id_value("321b8505-2f90-48a1"), "321b8505-2f90-48a1")
        self.assertEqual(sanitize_user_id_value("user@example.com"), "user_example_com")
        self.assertEqual(sanitize_user_id_value("///"), "claude_desktop")

    def test_sanitize_user_ids_recurses(self) -> None:
        payload = {
            "metadata": {"user_id": "user@example.com"},
            "messages": [{"userid": "bad/value"}],
            "unchanged": "bad/value",
        }
        self.assertEqual(
            sanitize_user_ids(payload),
            {
                "metadata": {"user_id": "user_example_com"},
                "messages": [{"userid": "bad_value"}],
                "unchanged": "bad/value",
            },
        )

    def test_estimate_input_tokens_counts_nested_content(self) -> None:
        self.assertGreater(
            estimate_input_tokens({"messages": [{"content": "hello world"}]}),
            0,
        )
        self.assertEqual(estimate_input_tokens(None), 0)

    def test_rewrite_model_routes_maps_claude_routes_to_deepseek(self) -> None:
        self.assertEqual(
            rewrite_model_routes_with_map({"model": "claude-sonnet-4-5"}, {"claude-sonnet-4-5": "deepseek-v4-pro"}),
            {"model": "deepseek-v4-pro"},
        )
        self.assertEqual(
            rewrite_model_routes_with_map(
                {"model": "anthropic/claude-sonnet-4-5[1m]"},
                {"claude-sonnet-4-5": "deepseek-v4-flash"},
            ),
            {"model": "deepseek-v4-flash"},
        )
        self.assertEqual(
            rewrite_model_routes({"model": "deepseek-v4-pro"}),
            {"model": "deepseek-v4-pro"},
        )

    def test_parse_model_maps_builds_label_and_route_map(self) -> None:
        models, route_map = parse_model_maps(
            [
                "Claude Mythos Flash=deepseek-v4-flash",
                "Claude Mythos=deepseek-v4-pro",
            ]
        )
        self.assertEqual(
            models,
            [
                {"name": "claude-sonnet-4-5", "labelOverride": "Claude Mythos Flash"},
                {"name": "anthropic/claude-sonnet-4-5", "labelOverride": "Claude Mythos"},
            ],
        )
        self.assertEqual(
            route_map,
            {
                "claude-sonnet-4-5": "deepseek-v4-flash",
                "anthropic/claude-sonnet-4-5": "deepseek-v4-pro",
            },
        )
        self.assertEqual(
            [model["name"] for model in models],
            list(route_map.keys()),
        )

    def test_apply_cowork_auto_mode_config_adds_managed_mcp_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude_desktop_config.json"
            path.write_text(
                json.dumps(
                    {
                        "preferences": {
                            "bypassPermissionsGateByAccount": {
                                "account-id": False,
                            }
                        },
                        "mcpServers": {
                            "chrome-devtools": {
                                "command": "/node",
                                "args": ["mcp"],
                                "env": {"PATH": "/bin"},
                                "alwaysAllow": ["wait_for", "click"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            apply_cowork_auto_mode_config(path, dry_run=False)

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIs(data["autoModeEnabled"], True)
            self.assertIs(data["preferences"]["bypassPermissionsGateByAccount"]["account-id"], True)
            self.assertEqual(
                data["managedMcpServers"],
                [
                    {
                        "name": "chrome-devtools",
                        "transport": "stdio",
                        "command": "/node",
                        "args": ["mcp"],
                        "env": {"PATH": "/bin"},
                        "toolPolicy": {
                            "wait_for": "allow",
                            "click": "allow",
                        },
                    }
                ],
            )

    def test_apply_profile_writes_auto_mode_and_managed_mcp_servers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed_mcp_servers = [
                {
                    "name": "chrome-devtools",
                    "transport": "stdio",
                    "command": "/node",
                    "toolPolicy": {"click": "allow"},
                }
            ]

            apply_profile(
                third_party_root=root,
                api_key="sk-test",
                base_url="http://127.0.0.1:17631",
                auth_scheme="bearer",
                models=[{"name": "claude-sonnet-4-5"}],
                managed_mcp_servers=managed_mcp_servers,
                dry_run=False,
            )

            profile_path = root / "configLibrary" / "00000000-0000-4000-8000-00000000d335.json"
            data = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertIs(data["autoModeEnabled"], True)
            self.assertEqual(data["managedMcpServers"], managed_mcp_servers)


if __name__ == "__main__":
    unittest.main()
