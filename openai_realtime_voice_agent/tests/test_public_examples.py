"""This repository is a public surface: nothing private may be committed.

Scans every tracked file (code, docs, examples, demo, translations) for private
LAN addresses, e-mail addresses, credential-shaped strings, coding-session
URLs or assistant attribution, and tracked audio or voice data. Names of real
people cannot be listed in a public test, so maintainers point
VOICEPE_PRIVATE_DENYLIST at a local, untracked file (one case-insensitive term
per line) to include them.
"""
import os
import re
import subprocess
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = REPOSITORY_ROOT / "examples" / "openclaw-bridge" / "bridge.mjs"

TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".mjs", ".js", ".sh", ".txt", ".toml",
                 ".cfg", ".html", ".plist", ".service", ""}
PATTERNS = {
    "private IPv4 address (use 192.0.2.0/24 in examples)": re.compile(
        r"(?<![\d.])(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![\d.])"),
    "e-mail address": re.compile(
        r"[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com)(?!example\.(?:com|org|net)\b)"
        r"[A-Za-z0-9-]+\.[A-Za-z0-9.-]*[A-Za-z]{2,}"),
    "credential-shaped string": re.compile(
        r"(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|eyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]+)"),
    "coding-session URL or assistant attribution": re.compile(
        r"(claude\.ai/code|Co-Authored-By:\s*Claude)", re.I),
}
VOICE_DATA = re.compile(
    r"(\.(wav|flac|mp3|m4a|aac|ogg|opus|webm|npy|npz|pkl|pt|pth|onnx|tflite)$"
    r"|(^|/)(voice-enrollment|voice-prints|voice-probes|voice-memory|recordings)/"
    r"|detection_calibration\.json$)", re.I)


def tracked_files():
    # Include new files before they are staged. Privacy checks are most useful
    # before a release commit, not only after its contents have entered Git.
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def text_lines(rel):
    path = REPOSITORY_ROOT / rel
    if Path(rel).suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return []


class TestPublicSurface(unittest.TestCase):
    def test_no_private_addresses_emails_credentials_or_attribution(self):
        problems = []
        for rel in tracked_files():
            for number, line in enumerate(text_lines(rel), 1):
                for label, pattern in PATTERNS.items():
                    if pattern.search(line):
                        problems.append(f"{rel}:{number}: {label}")
        self.assertEqual(problems, [])

    def test_no_audio_or_voice_data_is_tracked(self):
        self.assertEqual([rel for rel in tracked_files() if VOICE_DATA.search(rel)], [])

    def test_private_denylist(self):
        path = os.environ.get("VOICEPE_PRIVATE_DENYLIST", "")
        if not path:
            self.skipTest("set VOICEPE_PRIVATE_DENYLIST to a local, untracked list of private terms")
        terms = [t.strip().lower() for t in Path(path).read_text(encoding="utf-8").splitlines()
                 if t.strip() and not t.startswith("#")]
        hits = []
        for rel in tracked_files():
            for number, line in enumerate(text_lines(rel), 1):
                lowered = line.lower()
                hits.extend(f"{rel}:{number}" for term in terms if term in lowered)
        self.assertEqual(hits, [], "private terms found (terms not shown)")


class TestPublicExamples(unittest.TestCase):
    def test_private_imessage_routes_are_gitignored(self):
        result = subprocess.run(
            ["git", "check-ignore", "examples/openclaw-bridge/.imessage-routes.json"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bridge_loads_imessage_routes_from_private_configuration(self):
        source = BRIDGE.read_text(encoding="utf-8")
        self.assertIn('readOptional(".imessage-routes.json")', source)
        self.assertIn("process.env.IMESSAGE_ROUTES", source)
        self.assertNotIn("new Map([", source)
        self.assertIsNone(re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", source))
        self.assertIsNone(re.search(r"(?<!\w)\+?\d{10,15}(?!\w)", source))


if __name__ == "__main__":
    unittest.main()
