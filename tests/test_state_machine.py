import unittest

from agent_team.state_machine import (
    RUNNING_PHASES,
    agent_phase_for_running_phase,
    default_next_phase,
    human_input_resume_phase_for_agent_phase,
    is_running_phase,
    ready_phase_for_agent_phase,
    ready_phase_for_running_phase,
    runnable_phase_for,
    validate_human_input_resume_phase,
    validate_transition,
)


class StateMachineTests(unittest.TestCase):
    def test_valid_transition(self) -> None:
        validate_transition("needs_research", "researching")

    def test_draft_can_only_publish_to_research(self) -> None:
        validate_transition("draft", "needs_research")
        self.assertIsNone(runnable_phase_for("draft"))
        with self.assertRaises(ValueError):
            validate_transition("draft", "ready_for_plan")
        with self.assertRaises(ValueError):
            validate_transition("draft", "blocked")

    def test_plan_rejection_transition_is_valid(self) -> None:
        validate_transition("awaiting_plan_approval", "ready_for_plan")

    def test_plan_source_change_requeue_transition_is_valid(self) -> None:
        validate_transition("planning", "ready_for_plan")

    def test_invalid_transition_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_transition("needs_research", "done")

    def test_reset_to_draft_is_not_a_generic_transition(self) -> None:
        for phase in (
            "needs_research",
            "ready_for_plan",
            "awaiting_plan_approval",
            "ready_for_implementation",
            "ready_for_validation",
            "ready_for_review",
            "awaiting_merge_approval",
            "blocked",
            "done",
        ):
            with self.subTest(phase=phase):
                with self.assertRaises(ValueError):
                    validate_transition(phase, "draft")

    def test_runnable_phase(self) -> None:
        self.assertEqual(runnable_phase_for("ready_for_plan"), "plan")
        self.assertIsNone(runnable_phase_for("planning"))

    def test_ready_phase_for_agent_phase(self) -> None:
        expected_ready = {
            "research": "needs_research",
            "plan": "ready_for_plan",
            "implementation": "ready_for_implementation",
            "validation": "ready_for_validation",
            "review": "ready_for_review",
            "merge": "ready_for_merge",
            "merge_conflict_resolution": "ready_for_merge_conflict_resolution",
        }
        for agent_phase, ready_phase in expected_ready.items():
            with self.subTest(agent_phase=agent_phase):
                self.assertEqual(ready_phase_for_agent_phase(agent_phase), ready_phase)
        self.assertIsNone(ready_phase_for_agent_phase("unknown"))

    def test_review_enters_merge_approval_gate(self) -> None:
        validate_transition("reviewing", "awaiting_merge_approval")
        validate_transition("reviewing", "ready_for_implementation")
        with self.assertRaises(ValueError):
            validate_transition("reviewing", "done")
        self.assertEqual(default_next_phase("review"), "awaiting_merge_approval")

    def test_merge_approval_and_merge_transitions_are_valid(self) -> None:
        validate_transition("awaiting_merge_approval", "ready_for_merge")
        validate_transition("ready_for_merge", "merging")
        validate_transition("merging", "done")
        self.assertIsNone(runnable_phase_for("awaiting_merge_approval"))
        self.assertEqual(runnable_phase_for("ready_for_merge"), "merge")
        self.assertEqual(default_next_phase("merge"), "done")

    def test_merge_conflict_resolution_transitions_are_valid(self) -> None:
        validate_transition("merging", "ready_for_merge_conflict_resolution")
        validate_transition("ready_for_merge_conflict_resolution", "resolving_merge_conflicts")
        validate_transition("resolving_merge_conflicts", "ready_for_validation")
        self.assertEqual(runnable_phase_for("ready_for_merge_conflict_resolution"), "merge_conflict_resolution")
        self.assertEqual(default_next_phase("merge_conflict_resolution"), "ready_for_validation")

    def test_running_phase_helpers_map_back_to_ready_phases(self) -> None:
        expected_ready = {
            "researching": "needs_research",
            "planning": "ready_for_plan",
            "implementing": "ready_for_implementation",
            "validating": "ready_for_validation",
            "reviewing": "ready_for_review",
            "merging": "ready_for_merge",
            "resolving_merge_conflicts": "ready_for_merge_conflict_resolution",
        }
        for agent_phase, running_phase in RUNNING_PHASES.items():
            with self.subTest(running_phase=running_phase):
                self.assertTrue(is_running_phase(running_phase))
                self.assertEqual(agent_phase_for_running_phase(running_phase), agent_phase)
                self.assertEqual(ready_phase_for_running_phase(running_phase), expected_ready[running_phase])

        self.assertFalse(is_running_phase("ready_for_plan"))
        self.assertIsNone(agent_phase_for_running_phase("ready_for_plan"))
        self.assertIsNone(ready_phase_for_running_phase("ready_for_plan"))

    def test_recovery_reverse_transitions_are_not_manual_transitions(self) -> None:
        with self.assertRaises(ValueError):
            validate_transition("implementing", "ready_for_implementation")

    def test_human_input_is_non_runnable_waiting_phase(self) -> None:
        self.assertIsNone(runnable_phase_for("awaiting_human_input"))
        self.assertFalse(is_running_phase("awaiting_human_input"))

    def test_agent_running_phases_can_request_human_input_but_merge_cannot(self) -> None:
        for phase in (
            "researching",
            "planning",
            "implementing",
            "validating",
            "reviewing",
            "resolving_merge_conflicts",
        ):
            with self.subTest(phase=phase):
                validate_transition(phase, "awaiting_human_input")
        with self.assertRaises(ValueError):
            validate_transition("merging", "awaiting_human_input")

    def test_human_input_resume_phase_validation(self) -> None:
        expected = {
            "research": "needs_research",
            "plan": "ready_for_plan",
            "implementation": "ready_for_implementation",
            "validation": "ready_for_validation",
            "review": "ready_for_review",
            "merge_conflict_resolution": "ready_for_merge_conflict_resolution",
        }
        for agent_phase, resume_phase in expected.items():
            with self.subTest(agent_phase=agent_phase):
                self.assertEqual(human_input_resume_phase_for_agent_phase(agent_phase), resume_phase)
                validate_human_input_resume_phase(agent_phase, resume_phase)
                validate_transition("awaiting_human_input", resume_phase)
        with self.assertRaises(ValueError):
            validate_human_input_resume_phase("implementation", "ready_for_review")


if __name__ == "__main__":
    unittest.main()
