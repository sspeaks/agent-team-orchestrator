import unittest

from agent_team.blocked_summary import BLOCKED_SUMMARY_FALLBACK, summarize_blocked_reason


class BlockedSummaryTests(unittest.TestCase):
    def test_strips_markdown_routing_and_stack_trace_noise(self) -> None:
        summary = summarize_blocked_reason(
            """
            # Blocked reason

            Blocked summary: The source checkout is missing credentials. Add the credentials and rerun research.

            Traceback (most recent call last):
              File "runner.py", line 1, in <module>

            Recommendation: `blocked`
            """
        )

        self.assertEqual(summary, "The source checkout is missing credentials. Add the credentials and rerun research.")

    def test_limits_to_two_sentences_and_truncates(self) -> None:
        summary = summarize_blocked_reason(
            "First sentence explains the blocker. Second sentence explains the unblock step. "
            "Third sentence should not appear.",
            limit=90,
        )

        self.assertEqual(summary, "First sentence explains the blocker. Second sentence explains the unblock step.")

    def test_empty_text_uses_safe_fallback(self) -> None:
        self.assertEqual(summarize_blocked_reason("Recommendation: `blocked`"), BLOCKED_SUMMARY_FALLBACK)

    def test_preserves_identifier_underscores_when_stripping_markdown(self) -> None:
        summary = summarize_blocked_reason(
            "**config_path is missing.** Set ENV_VAR and rerun validation."
        )

        self.assertEqual(summary, "config_path is missing. Set ENV_VAR and rerun validation.")


if __name__ == "__main__":
    unittest.main()
