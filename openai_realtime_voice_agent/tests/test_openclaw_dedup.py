"""The direct agent route must be the only ask_openclaw tool the model sees."""
import unittest

from app.main import is_openclaw_mcp_duplicate


class TestOpenClawToolDedup(unittest.TestCase):
    def test_bare_and_namespaced_aliases_are_duplicates(self):
        self.assertTrue(is_openclaw_mcp_duplicate("ask_openclaw"))
        self.assertTrue(is_openclaw_mcp_duplicate("script__ask_openclaw"))
        self.assertTrue(is_openclaw_mcp_duplicate("mcp__ask_openclaw"))

    def test_other_scripts_remain_exposed(self):
        self.assertFalse(is_openclaw_mcp_duplicate("script__answer_person_eta"))
        self.assertFalse(is_openclaw_mcp_duplicate("recall_memory"))


if __name__ == "__main__":
    unittest.main()
