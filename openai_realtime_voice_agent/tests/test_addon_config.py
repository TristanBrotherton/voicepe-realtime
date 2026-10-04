"""The add-on's options, schema, translations, run.sh and app stay in sync.

A new option that is missing from the schema, the translations or run.sh, or
an exported variable the app never reads, is a silent no-op for users.
"""
import re
import unittest
from pathlib import Path

import yaml

ADDON = Path(__file__).resolve().parents[1]
# Exported for the Python runtime itself, not read by the app.
RUNTIME_ONLY = {"PYTHONUNBUFFERED"}


def _load(name):
    return yaml.safe_load((ADDON / name).read_text(encoding="utf-8"))


class TestAddonConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = _load("config.yaml")
        cls.config = config
        cls.options = config["options"]
        cls.schema = config["schema"]
        cls.translations = _load("translations/en.yaml")["configuration"]
        cls.run_sh = (ADDON / "root" / "run.sh").read_text(encoding="utf-8")
        cls.app_source = "\n".join(p.read_text(encoding="utf-8") for p in sorted((ADDON / "app").glob("*.py")))

    def test_every_option_has_a_schema_entry(self):
        self.assertEqual(sorted(set(self.options) - set(self.schema)), [])

    def test_required_schema_keys_have_defaults(self):
        required = [key for key, kind in self.schema.items() if not str(kind).endswith("?")]
        self.assertEqual(sorted(key for key in required if key not in self.options), [])

    def test_every_schema_key_is_translated(self):
        self.assertEqual([key for key in self.schema if key not in self.translations], [])
        for key in self.schema:
            entry = self.translations[key]
            self.assertTrue(entry.get("name") and entry.get("description"), key)

    def test_translations_have_no_stale_keys(self):
        self.assertEqual(sorted(set(self.translations) - set(self.schema)), [])

    def test_run_sh_reads_exactly_the_schema(self):
        read = set(re.findall(r"bashio::config(?:\.has_value)? '([a-z0-9_]+)'", self.run_sh))
        for loop in re.findall(r"for option in ([^;]+); do", self.run_sh):
            read |= set(loop.replace("\\", " ").split())
        self.assertEqual(sorted(set(self.schema) - read), [], "options run.sh never reads")
        self.assertEqual(sorted(read - set(self.schema)), [], "run.sh reads options that do not exist")

    def test_every_exported_variable_is_read_by_the_app(self):
        exported = set(re.findall(r"\bexport ([A-Z][A-Z0-9_]*)", self.run_sh)) - RUNTIME_ONLY
        unread = sorted(name for name in exported if f'"{name}"' not in self.app_source)
        self.assertEqual(unread, [])

    def test_every_option_is_in_the_configuration_reference(self):
        reference = (ADDON.parent / "docs" / "configuration.md").read_text(encoding="utf-8")
        documented = set(re.findall(r"^\| `([a-z0-9_]+)`", reference, re.M))
        self.assertEqual(sorted(set(self.schema) - documented), [])

    def test_new_privacy_defaults_are_conservative(self):
        self.assertFalse(self.options["log_transcripts"])
        self.assertFalse(self.options["trigger_capture"])
        self.assertFalse(self.options["enable_recording"])
        self.assertEqual(self.options["wake_capture"], "auto")
        self.assertEqual(self.schema["device_token"], "password")


if __name__ == "__main__":
    unittest.main()
