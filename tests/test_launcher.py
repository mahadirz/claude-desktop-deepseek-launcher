import unittest

from claude_deepseek_launcher.__main__ import (
    estimate_input_tokens,
    normalize_api_key,
    sanitize_user_id_value,
    sanitize_user_ids,
)


class LauncherHelpersTest(unittest.TestCase):
    def test_normalize_api_key_strips_bearer_prefix(self) -> None:
        self.assertEqual(normalize_api_key("Bearer sk-test"), "sk-test")
        self.assertEqual(normalize_api_key("bearer sk-test"), "sk-test")
        self.assertEqual(normalize_api_key("sk-test"), "sk-test")

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


if __name__ == "__main__":
    unittest.main()
