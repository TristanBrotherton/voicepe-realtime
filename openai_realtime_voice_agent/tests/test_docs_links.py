"""Relative links and #anchors in the repository's Markdown resolve.

Headings change; a renamed section silently breaks every link to it. This
checks each relative link's target file and, for #fragments, a GitHub-style
heading slug in that file. External URLs are not fetched.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$", re.M)
FENCE = re.compile(r"^```.*?^```", re.M | re.S)


def slug(heading: str) -> str:
    text = re.sub(r"`|\*\*|\*|_", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    return text.replace(" ", "-")


def anchors(path: Path) -> set:
    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    seen, out = {}, set()
    for heading in HEADING.findall(text):
        base = slug(heading)
        count = seen.get(base, 0)
        out.add(base if count == 0 else f"{base}-{count}")
        seen[base] = count + 1
    return out


def markdown_files():
    out = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True)
    return [ROOT / line for line in out.stdout.splitlines() if line]


class TestDocsLinks(unittest.TestCase):
    def test_relative_links_and_anchors_resolve(self):
        broken = []
        for md in markdown_files():
            text = FENCE.sub("", md.read_text(encoding="utf-8"))
            for target in LINK.findall(text):
                if re.match(r"^[a-z]+:", target) or target.startswith("<"):
                    continue
                file_part, _, fragment = target.partition("#")
                dest = (md.parent / file_part).resolve() if file_part else md
                if not dest.exists():
                    broken.append(f"{md.relative_to(ROOT)}: {target} (missing file)")
                    continue
                if fragment and dest.suffix == ".md" and fragment not in anchors(dest):
                    broken.append(f"{md.relative_to(ROOT)}: {target} (missing anchor)")
        self.assertEqual(broken, [])

    def test_slug_matches_github(self):
        self.assertEqual(slug("What about privacy — what leaves my network?"),
                         "what-about-privacy--what-leaves-my-network")
        self.assertEqual(slug("2.5 Lock the device connection (recommended)"),
                         "25-lock-the-device-connection-recommended")
        self.assertEqual(slug("Confirmations & device access"), "confirmations--device-access")


if __name__ == "__main__":
    unittest.main()
