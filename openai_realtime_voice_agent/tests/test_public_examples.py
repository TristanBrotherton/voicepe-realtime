"""This repository is a public surface: nothing private may be committed.

Three checks, each deliberately narrow and each described by what it does:

* ``TestPublicSurface`` scans every file git considers source for private LAN
  addresses, e-mail addresses, credential-shaped strings, coding-session URLs
  or assistant attribution, tracked audio, and the owner's private terms. The
  terms come from ``VOICEPE_PRIVATE_DENYLIST``, a local untracked file, because
  the names of real people cannot be listed in a public test. A green denylist
  scan means the declared terms are absent, and nothing more.
* ``TestPrivateTermScanIsLoadBearing`` shows that scan can find a planted term
  and that it reaches the runtime and the tests.
* ``TestSpeakerFixturesAreFictional`` keeps test data that has the shape of a
  household record, a given name beside a voice-type label, drawn from one
  closed fictional pool. It is a tripwire for the obvious ways a name is
  written into a Python source as a string literal. It is not a name detector.
  Its scope and known limits are in its class docstring.
"""
import ast
import os
import re
import subprocess
import unicodedata
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = REPOSITORY_ROOT / "examples" / "openclaw-bridge" / "bridge.mjs"

# The fictional names the suite may use for people. Add one deliberately, with
# a reason in the pull request.
SPEAKER_FIXTURE_NAMES = frozenset({
    "sam", "alex", "morgan", "priya", "jordan", "wei", "robin", "kim",
})

# Words with a name's shape that are not people: Home Assistant areas and
# device names, spoken-reply fixtures, protocol words. Each carries a one-line
# reason. `test_the_non_name_allowlist_is_minimal_and_reasoned` requires every
# entry to be in use somewhere in the scanned sources, so a real name cannot be
# parked here ahead of the fixture that would spend it.
NON_NAME_LITERALS = {
    'Colour Bulb': 'Home Assistant device name in the action-gate fixtures',
    'Accept': 'HTTP header name in the Home Assistant MCP request (app/ha_household.py)',
    'Assist': "name of Home Assistant's MCP prompt (app/ha_household.py)",
    'Mcp-Session-Id': 'MCP streamable-HTTP session header (app/ha_household.py)',
    'Current': 'first word of the date/time line given to the voice and the router backend',
    'Milk': 'list item in the acknowledgement classifier docstring',
    'Sure': 'first word of an acknowledgement phrase',
    'Okay': 'first word of an acknowledgement phrase',
    'Alright': 'first word of an acknowledgement phrase',
    'Yes': 'a bare yes/no answer the pre-result gate must hold',
    'Absolutely': 'an affirmative the pre-result gate must hold',
    'Saturday': 'weekday: a capitalised answer the pre-result gate must hold',
    'Tuesday': 'weekday: a capitalised answer the pre-result gate must hold',
    'October': 'month: a capitalised answer the pre-result gate must hold',
    'Finished': 'outcome word the pre-result gate must hold',
    'Complete': 'outcome word the pre-result gate must hold',
    'Enabled': 'outcome word the pre-result gate must hold',
    'Verified': "first word of the router's result commentary",
    'Acknowledgement': 'spoken-phrase category in a revision fixture',
    'Added': 'verb opening a confirmation phrase',
    'Adding': 'verb opening a progress phrase',
    'Adjusting': 'verb opening a progress phrase',
    'Atrium Lamp': 'Home Assistant device name in the delegation fixtures',
    'Atrium': 'Home Assistant area name in the local-intent device-context fixtures',
    'Hey': 'interjection opening a spoken request in the read-only question fixtures',
    'Authorization': 'HTTP header name',
    'Bearer': 'HTTP authorization scheme',
    'Bounded': "first word of live_reply.py's module docstring",
    'Cedar': 'OpenAI voice id (tests/test_voice_runtime.py)',
    'Cheap': 'first word of a docstring in live_audio.py',
    'Coalescing': "first word of ha_sensors.py's class docstring",
    'Content-Type': 'HTTP header name',
    'Delegation': "section title of the OpenAI Live docs, listed in live_protocol.py's module docstring",
    'Done': "the model's one-word success claim in the false-success fixtures",
    'Entry Lock': 'Home Assistant device name in the delegation and kitchen-main fixtures',
    'Front Door': 'Home Assistant device name in the action-gate fixtures',
    'Garage Door': 'Home Assistant device name in the action-gate fixtures',
    'Generic': "first word of test_progress_ack.py's module docstring",
    'Hass': 'prefix of every Home Assistant intent name',
    'Home': "'Home Assistant', in a test docstring",
    'Home Alarm': 'Home Assistant device name in the action-gate fixtures',
    'Hz': "unit, in a sample-rate sentence in live_service.py's comments",
    'Kelvin': "unit, in tool_arguments.py's module docstring",
    'Kitchen': 'Home Assistant area; also the untouched control device',
    'Kitchen Light': 'Home Assistant device name in the action-gate fixtures',
    'Kitchen Main': 'Home Assistant device name in the action-gate fixtures',
    'Late': 'status word in a turn-timing fixture',
    'Light': 'Home Assistant domain and device class',
    'Lisbon': 'fixture town in the web-search and silent-web tests',
    'Live': "the GPT-Live runtime's own name",
    'Main': "thread name, and the 'Main' suffix of a device name",
    'Managing': "section title of the OpenAI Live docs, listed in live_protocol.py's module docstring",
    'Marking': 'verb opening a progress phrase',
    'Migrate': "section title of the OpenAI Live docs, listed in live_protocol.py's module docstring",
    'Missing': 'first word of a comment sentence in live_service.py',
    'One': 'first word of "One moment." as a transcript delta in the render fixtures',
    'Next': "spoken enrollment prompt, 'Next, please.'",
    'No': 'refusal text in a response-payload fixture, and a negative clause in a web-search transcript',
    'None': 'the Python literal, written into docstrings in action_gate.py',
    'Office': 'Home Assistant area',
    'Office Blinds': 'Home Assistant device name in the action-gate fixtures',
    'Pipeline': "Pipecat concept, in websocket_handler.py's docstring",
    'Recorder': 'component name, in a retained-failure test docstring',
    'Removing': 'verb opening a progress phrase',
    'Scripted': "first word of the demo module's docstring",
    'Sorry': 'first word of the rate-limit spoken prompt',
    'Stopping': 'verb opening a progress phrase',
    'Sunny': "weather answer in a web-search fixture, 'Sunny, 21 degrees.'",
    'Tap': "pipeline component name, in live_harness.py's docstring",
    'Timers': 'subject of the Home Assistant timer intents',
    'Twelve': 'spelled-out number in a spoken-reply fixture',
    'Unset': "first word of openclaw_tool.py's module docstring",
    'Vacation': 'todo-list name in a list fixture',
    'Vacation Lamp': 'Home Assistant device name in the action-gate fixtures',
    'Vacation Mode': 'Home Assistant helper name in the action-gate fixtures',
    'Vesper': 'OpenAI voice id (tests/test_voice_runtime.py)',
    'Warm': "first word of the voice-style instruction, 'Warm, clear and concise.'",
    'Workshop': 'Home Assistant area; also the device under test',
    'Workshop Lamp': 'Home Assistant device name in a tool-result fixture',
    'Workshop Main': 'Home Assistant device name in the action-gate fixtures',
}

# Attributes and parameters that hold a speaker's name. ``speaker_name`` is
# the real keyword of ``app.progress_ack.progress_phrase`` and was missing
# here, so the ordinary supported call
# ``progress_phrase(tool_name=..., arguments=..., speaker_name="Alice")``
# carried an off-pool name straight through a green gate.
SPEAKER_NAME_SLOTS = frozenset({
    "current_name", "male_name", "female_name", "speaker_name",
    "speaker_male_name", "speaker_female_name",
})

# The labels a speaker-recognition mapping keys a name by. A mapping is read
# key by key rather than by its whole key set, because an unrelated extra key
# does not stop ``{"male": ..., "female": ...}`` from being a household record.
VOICE_TYPE_KEYS = frozenset({"male", "female"})

# Calls whose positional arguments carry a speaker's name, and the first
# position that does: ``SpeakerProbe(male_name, female_name)`` and
# ``progress_phrase(tool_name, arguments, speaker_name, transcript)``.
SPEAKER_CALL_POSITIONS = {"SpeakerProbe": 0, "progress_phrase": 2}

# How the add-on personalizes a spoken phrase: "<phrase>, <Name>." A literal
# of that shape is a person's name whatever the surrounding code looks like.
PERSONALIZED_PHRASE = re.compile(r",\s+([A-Z][a-z]+)\.")

# A name word: Unicode title case with any number of internal hyphens or
# apostrophes. ``str.isalpha`` and ``str.isupper`` are Unicode-aware, so
# ``José``, ``Zoë``, ``Иван``, ``O'Brien`` and ``Jean-Pierre-Marie`` are words.
NAME_MARKS = "-'’"
_MARK_SPLIT = re.compile("[" + re.escape(NAME_MARKS) + "]")

# Format and zero-width controls (Cf) and combining marks (Mn, Mc, Me). A word
# carrying one cannot equal any pool entry, so it is not read as a name; the
# owner-declared denylist is the check for a name written that way.
_INVISIBLE_OR_COMBINING = frozenset({"Cf", "Mn", "Mc", "Me"})

# A name used in address, in both forms: ``", Alice"`` and ``"Alice,"``.
ADDRESS_FORMS = (
    re.compile(r",\s+([^\W\d_][^\W\d_" + re.escape(NAME_MARKS) + r"]*)"),
    re.compile(r"([^\W\d_][^\W\d_" + re.escape(NAME_MARKS) + r"]*),"),
)

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


def denylist_hits(terms, rel, lines):
    """``rel:line`` for every private term found, case-insensitively.

    Factored out so the scan itself is testable: a check that reports "no
    hits" is only worth having if it can be shown to find a planted one.
    """
    hits = []
    for number, line in enumerate(lines, 1):
        lowered = line.lower()
        hits.extend(f"{rel}:{number}" for term in terms if term in lowered)
    return hits


def _string_literals(nodes):
    """Every string constant reachable in ``nodes`` as a literal.

    Descends through sequence displays and ``*`` splats, because
    ``SpeakerProbe(*("Alice", "Bob"))`` and ``probe.male_name, probe.female_name
    = "Alice", "Bob"`` are ordinary call and assignment forms that have to read
    the same as their flat spellings. They did not, and that was a bypass.
    """
    found = set()
    for node in nodes:
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            found |= _string_literals(node.elts)
        elif isinstance(node, ast.Starred):
            found |= _string_literals([node.value])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
    return found


def _literal_strings(tree):
    """Every string literal in ``tree``, including f-string and bytes literals.

    ``f"Alice"`` substitutes nothing, but CPython still parses it as a
    ``JoinedStr``; skipping those is how an f-string with no substitution hid
    a plain name from candidate-v17's scan. ``b"Alice"`` is a literal too, and
    skipping it is how `b"Alice".decode()` read clean in candidate-v18.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, ast.Constant) and isinstance(node.value, bytes):
            try:
                yield node.value.decode("ascii")
            except UnicodeDecodeError:
                pass    # binary fixture, not text
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    yield part.value


def _is_name_word(word):
    """One word with a name's shape.

    Title case by Unicode letter class, at least two characters, letters and
    internal hyphens or apostrophes only, no digit, no invisible or combining
    character, and no mark at either edge. Lowercase, ALL-CAPS and internal
    capitals are not read: ``alice``, ``ALICE`` and ``McKenzie`` are left to the
    denylist, which is where the line between a tripwire and ordinary prose is.
    """
    if len(word) < 2:
        return False
    if any(unicodedata.category(c) in _INVISIBLE_OR_COMBINING or c.isspace()
           or c.isdigit() or c.isnumeric() for c in word):
        return False
    if word[0] in NAME_MARKS or word[-1] in NAME_MARKS:
        return False
    for part in _MARK_SPLIT.split(word):
        if not part.isalpha():
            return False
        if not part[0].isupper() or (len(part) > 1 and not part[1:].islower()):
            return False
    return True


def _is_name_initial(word):
    """``B.`` inside a multi-word phrase, as in ``Alice B. Smith``."""
    return (len(word) == 2 and word[1] == "."
            and word[0].isalpha() and word[0].isupper())


def _is_name_phrase(text):
    """A sequence of name words, with one-letter initials allowed inside it."""
    words = text.split(" ")
    if len(words) == 1:
        return _is_name_word(words[0])
    return all(_is_name_initial(word) or _is_name_word(word) for word in words)


def is_pooled(literal):
    """Whether every name word in ``literal`` comes from the declared pool.

    Word by word, so a pool-only full name such as ``"Sam Morgan"`` is clean
    while ``"Sam Smith"`` still has an off-pool word and is an offender.
    """
    words = [word for word in literal.split(" ")
             if not _is_name_initial(word)]
    return bool(words) and all(word.lower() in SPEAKER_FIXTURE_NAMES
                               for word in words)


def name_shaped_literals(source):
    """Every name-shaped literal in a Python source, wherever it is written.

    A string, f-string or bytes literal that, stripped of surrounding
    whitespace, is entirely a sequence of name words, or a name word in either
    address position. Position in the source does not matter, which is what
    closes the indirections (a loop, a module constant, a ``dict()`` call, an
    environment assignment, a default parameter) that a slot-based scan cannot
    follow.

    What this does not read, stated plainly: comments, identifiers, strings
    computed or formatted at runtime, files that are not Python, a name inside
    a longer sentence, lowercase or ALL-CAPS spellings, words with an internal
    capital, a digit, or a zero-width character. The owner-declared denylist is
    the check for a real name in any of those places.
    """
    found = set()
    for text in _literal_strings(ast.parse(source)):
        stripped = text.strip()
        if _is_name_phrase(stripped):
            found.add(stripped)
        for pattern in ADDRESS_FORMS:
            found |= {match.group(1) for match in pattern.finditer(text)
                      if _is_name_word(match.group(1))}
    return found


def _assigned_slots(targets):
    """The names ``targets`` assigns to, descending into tuple unpacking."""
    slots = set()
    for target in targets:
        if isinstance(target, (ast.Tuple, ast.List)):
            slots |= _assigned_slots(target.elts)
        else:
            name = getattr(target, "attr", None) or getattr(target, "id", None)
            if name:
                slots.add(name)
    return slots


def _positional_speaker_names(node, first):
    """Names in the positional arguments of a known speaker call.

    A ``*`` splat makes the argument positions unknowable from the syntax, so
    every literal in the call is read instead of guessing an offset. That
    over-reads rather than under-reads: this gate has to fail closed.
    """
    if any(isinstance(arg, ast.Starred) for arg in node.args):
        return _string_literals(node.args)
    return _string_literals(node.args[first:])


def speaker_names_in(source):
    """Which speaker slot a literal name is written into.

    Reads a literal only where it is written directly into a known position:
    the positional arguments of ``SpeakerProbe`` and ``progress_phrase``
    (every literal in the call when a ``*`` splat makes positions unknowable),
    any keyword or ``**`` mapping key naming a slot in ``SPEAKER_NAME_SLOTS``,
    any keyword to ``SpeakerProbe``, a mapping value keyed by a voice-type
    label, an assignment into a slot (plain, annotated, walrus or unpacked),
    and the name a progress phrase is personalized with, ``", <Name>."``.

    A name that reaches a slot through a variable, loop, helper, alias or
    default is not read here. ``name_shaped_literals`` is the check that does
    not depend on position.
    """
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            called = (func.attr if isinstance(func, ast.Attribute)
                      else getattr(func, "id", ""))
            if called in SPEAKER_CALL_POSITIONS:
                found |= _positional_speaker_names(
                    node, SPEAKER_CALL_POSITIONS[called])
            if called == "SpeakerProbe":
                # Every parameter it takes is a name slot.
                found |= _string_literals([kw.value for kw in node.keywords])
            for keyword in node.keywords:
                if keyword.arg in SPEAKER_NAME_SLOTS:
                    found |= _string_literals([keyword.value])
                elif keyword.arg is None and isinstance(keyword.value, ast.Dict):
                    # progress_phrase(**{"speaker_name": "Alice"})
                    for key, value in zip(keyword.value.keys,
                                          keyword.value.values):
                        if (isinstance(key, ast.Constant)
                                and key.value in SPEAKER_NAME_SLOTS):
                            found |= _string_literals([value])
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in VOICE_TYPE_KEYS:
                    found |= _string_literals([value])
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            if (_assigned_slots(targets) & SPEAKER_NAME_SLOTS
                    and node.value is not None):
                found |= _string_literals([node.value])
        elif isinstance(node, ast.NamedExpr):
            if getattr(node.target, "id", None) in SPEAKER_NAME_SLOTS:
                found |= _string_literals([node.value])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found |= set(PERSONALIZED_PHRASE.findall(node.value))
    return {name for name in found if name}


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
            hits.extend(denylist_hits(terms, rel, text_lines(rel)))
        self.assertEqual(hits, [], "private terms found (terms not shown)")


class TestPrivateTermScanIsLoadBearing(unittest.TestCase):
    """A clean private-term scan only means something if the scan can fail.

    Its one check read a local denylist and skipped itself when that was
    unset, so every release so far passed a privacy gate that had inspected
    nothing. These are the properties a green result depends on, and none of
    them needs a private term to assert.
    """

    RUNTIME = "openai_realtime_voice_agent/app/progress_ack.py"

    def test_a_planted_term_is_found_whatever_its_case(self):
        self.assertEqual(
            denylist_hits(["someone", "somewhere"], "x.py",
                          ["clean", "Someone lives here", "", "in SOMEWHERE"]),
            ["x.py:2", "x.py:4"])
        self.assertEqual(denylist_hits(["someone"], "x.py", ["clean"]), [])

    def test_the_scan_reaches_the_runtime_and_the_tests(self):
        """Scanning the wrong files is how a leak survives a green gate."""
        scanned = tracked_files()
        self.assertIn(self.RUNTIME, scanned)
        self.assertTrue(text_lines(self.RUNTIME),
                        "the runtime module read back empty; the suffix "
                        "filter or the path is wrong")
        tests = [rel for rel in scanned
                 if rel.startswith("openai_realtime_voice_agent/tests/")]
        self.assertGreater(len(tests), 40)
        self.assertTrue(all(text_lines(rel) for rel in tests))

    def test_the_runtime_names_no_installation_specific_script_ids(self):
        """Household script ids are not public defaults.

        A fixture may name a tool; a shipped default may not, because the
        add-on runs in houses that have never heard of this one's scripts.
        The person-location cue and the car-climate cue are both derived from
        shape now, so nothing needs an id here.
        """
        app = REPOSITORY_ROOT / "openai_realtime_voice_agent" / "app"
        offenders = [
            f"{source.relative_to(REPOSITORY_ROOT)}:{number}"
            for source in sorted(app.rglob("*.py"))
            for number, line in enumerate(
                source.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"\btesla_[a-z0-9_]+", line)
        ]
        self.assertEqual(offenders, [])


class TestSpeakerFixturesAreFictional(unittest.TestCase):
    """The names in the public suite come from a declared pool.

    Speaker recognition is the one feature whose fixtures have the shape of a
    real household record, a given name beside a voice-type label, and the
    maintainer's own given name was once used that way. The denylist cannot
    hold what nobody declared, so the suite draws its people from one closed
    fictional pool and these checks hold it there.

    Scope: the string, f-string and bytes literals of every Python source git
    lists, excluding this module. Two readers: ``speaker_names_in`` for the
    known speaker slots and calls, and ``name_shaped_literals`` for any literal
    that is entirely a title-case name or a name in address position. Each name
    found must be a pool name or a declared non-name.

    Known limits: comments, identifiers, computed strings, non-Python files, a
    name inside a sentence, and lowercase, ALL-CAPS, internal-capital, digit or
    zero-width spellings are not read. This is a tripwire for the obvious ways
    a name is written into test data, not a name detector. The owner-declared
    denylist is the check for real names.
    """

    # This module has to spell out names from outside the pool to show the
    # scan finds them, exactly as the unshipped-tree check has to name the
    # trees it excludes.
    SELF = "openai_realtime_voice_agent/tests/test_public_examples.py"

    def sources(self):
        return [rel for rel in tracked_files()
                if rel.endswith(".py") and rel != self.SELF
                and (REPOSITORY_ROOT / rel).is_file()]

    def _offenders(self, source):
        return sorted(word for word in name_shaped_literals(source)
                      if not is_pooled(word)
                      and word not in NON_NAME_LITERALS)

    def test_this_module_is_the_only_exemption_and_it_is_real(self):
        self.assertIn(self.SELF, tracked_files())
        self.assertNotIn(self.SELF, self.sources())

    def test_every_speaker_fixture_name_is_from_the_declared_pool(self):
        offenders = []
        for rel in self.sources():
            source = (REPOSITORY_ROOT / rel).read_text(encoding="utf-8")
            offenders += [f"{rel}: {name}"
                          for name in sorted(speaker_names_in(source))
                          if not is_pooled(name)]
        self.assertEqual(
            offenders, [],
            "a speaker fixture names someone outside the declared pool in "
            "tests/test_public_examples.py; use a pool name, or add one "
            "deliberately and say why in the release program")

    def test_every_name_shaped_literal_is_pooled_or_a_declared_non_name(self):
        offenders = []
        for rel in self.sources():
            source = (REPOSITORY_ROOT / rel).read_text(encoding="utf-8")
            offenders += [f"{rel}: {word}" for word in self._offenders(source)]
        self.assertEqual(
            offenders, [],
            "a name-shaped literal is neither a pool name nor a declared "
            "non-name: use a name from SPEAKER_FIXTURE_NAMES, or, if it is "
            "not a person, add it to NON_NAME_LITERALS with the reason")

    def test_the_non_name_allowlist_is_minimal_and_reasoned(self):
        """An allowlist is a bypass unless it is narrow and in use."""
        self.assertEqual(
            [word for word, reason in NON_NAME_LITERALS.items()
             if not reason or not reason.strip()], [],
            "every non-name entry must say why it is not a person")
        self.assertEqual(
            sorted(set(NON_NAME_LITERALS) & {name.title()
                                             for name in SPEAKER_FIXTURE_NAMES}),
            [], "a pool name does not belong in the non-name allowlist")
        in_use = set()
        for rel in self.sources():
            in_use |= name_shaped_literals(
                (REPOSITORY_ROOT / rel).read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(set(NON_NAME_LITERALS) - in_use), [],
            "these non-name entries are no longer used anywhere; delete them "
            "rather than leaving the allowlist wider than the tree")

    # Every shape an independent review has used to get an off-pool name past
    # an earlier version of this gate, kept as the reviewers wrote them, plus
    # the full-name class that rejected candidate-v19. Each carries an off-pool
    # name as a plain literal and names the offender it must produce.
    REVIEWED_BYPASSES = (
        ('v16_keyword_speaker_name',
         'progress_phrase(tool_name="web_search", arguments={}, speaker_name="Alice")',
         'Alice'),
        ('v16_annotated_slot',
         'probe.current_name: str = "Alice"',
         'Alice'),
        ('v16_splatted_constructor',
         'SpeakerProbe(*("Alice", "Bob"))',
         'Alice'),
        ('v16_mapping_extra_key',
         'names = {"male": "Alice", "female": "Bob", "guest": "Carol"}',
         'Alice'),
        ('v17_dict_constructor',
         'names = dict(male="Alice", female="Bob")',
         'Alice'),
        ('v17_kw_mapping_variable',
         'kw = {"speaker_name": "Alice"}\nprogress_phrase(**kw)',
         'Alice'),
        ('v17_probe_kw_variable',
         'kw = {"male_name": "Alice", "female_name": "Bob"}\nSpeakerProbe(**kw)',
         'Alice'),
        ('v17_aliased_probe_import',
         'from app.speaker_context import SpeakerProbe as Probe\nprobe = Probe("Alice", "Bob")',
         'Alice'),
        ('v17_aliased_phrase_import',
         'from app.progress_ack import progress_phrase as pp\npp("tool", {}, "Alice")',
         'Alice'),
        ('v17_rebound_constructor',
         'make = SpeakerProbe\nmake("Alice", "Bob")',
         'Alice'),
        ('v17_partial',
         'functools.partial(SpeakerProbe, "Alice")("Bob")',
         'Alice'),
        ('v17_setattr',
         'setattr(probe, "current_name", "Alice")',
         'Alice'),
        ('v17_subscript_assign',
         'probe["current_name"] = "Alice"',
         'Alice'),
        ('v17_mapping_subscript_assign',
         'names["male"] = "Alice"',
         'Alice'),
        ('v17_conditional_expression',
         'probe.current_name = "Alice" if flag else "Bob"',
         'Alice'),
        ('v17_fstring_no_substitution_keyword',
         'progress_phrase(tool_name="t", arguments={}, speaker_name=f"Alice")',
         'Alice'),
        ('v17_fstring_no_substitution_positional',
         'SpeakerProbe(f"Alice", f"Bob")',
         'Alice'),
        ('v17_loop_over_literal_list',
         'for name in ["Alice", "Bob"]:\n    progress_phrase("tool", {}, name)',
         'Alice'),
        ('v17_comprehension',
         '[SpeakerProbe(n, n) for n in ("Alice", "Bob")]',
         'Alice'),
        ('v17_subtest_parametrization',
         'for name in ("Alice",):\n    probe.current_name = name',
         'Alice'),
        ('v17_module_constant_positional',
         'NAME = "Alice"\nSpeakerProbe(NAME, NAME)',
         'Alice'),
        ('v17_module_constant_keyword',
         'NAME = "Alice"\nprogress_phrase(speaker_name=NAME)',
         'Alice'),
        ('v17_dataclass_field_default',
         'current_name: str = field(default="Alice")',
         'Alice'),
        ('v17_patch_object_decorator',
         '@patch.object(SpeakerProbe, "current_name", "Alice")\ndef test_it(self):\n    pass',
         'Alice'),
        ('v17_default_parameter',
         'def fixture(speaker_name="Alice"):\n    pass',
         'Alice'),
        ('v17_lambda_default',
         'make = lambda speaker_name="Alice": None',
         'Alice'),
        ('v17_addon_options_mapping',
         'options = {"speaker_male_name": "Alice", "speaker_female_name": "Bob"}',
         'Alice'),
        ('v17_environment_assignment',
         'os.environ["SPEAKER_MALE_NAME"] = "Alice"',
         'Alice'),
        ('v17_patch_dict_environment',
         'patch.dict(os.environ, {"SPEAKER_MALE_NAME": "Alice"})',
         'Alice'),
        ('v17_splat_from_variable',
         'names = ("Alice", "Bob")\nSpeakerProbe(*names)',
         'Alice'),
        ('v17_forwarded_varargs',
         'def make(*args):\n    return SpeakerProbe(*args)\nmake("Alice", "Bob")',
         'Alice'),
        ('v17_nested_mapping',
         'cfg = {"male": {"name": "Alice"}}',
         'Alice'),
        ('v17_str_call',
         'progress_phrase("tool", {}, str("Alice"))',
         'Alice'),
        ('v17_boolean_fallback',
         'progress_phrase(speaker_name="Alice" or None)',
         'Alice'),
        ('v17_update_keyword',
         'names.update(male="Alice")',
         'Alice'),
        ('v17_setdefault',
         'names.setdefault("male", "Alice")',
         'Alice'),
        ('v17_dict_zip',
         'cfg = dict(zip(["male", "female"], ["Alice", "Bob"]))',
         'Alice'),
        ('v17_vars_subscript',
         'vars(probe)["current_name"] = "Alice"',
         'Alice'),
        ('v17_annotated_subscript',
         'names["male"]: str = "Alice"',
         'Alice'),
        ('v17_vocative_without_period',
         'msg = "Let me check that, Alice"',
         'Alice'),
        ('v17_constant_mapping_key',
         'MALE = "male"\ncfg = {MALE: "Alice"}',
         'Alice'),
        ('v17_dict_from_pairs',
         'cfg = dict([("male", "Alice"), ("female", "Bob")])',
         'Alice'),
        ('v18_accented_letter',
         'NAME = "José"\nprobe = SpeakerProbe(NAME, NAME)',
         'José'),
        ('v18_diaeresis',
         'NAME = "Zoë"\nprobe = SpeakerProbe(NAME, NAME)',
         'Zoë'),
        ('v18_non_latin_script',
         'NAME = "Иван"\nprobe = SpeakerProbe(NAME, NAME)',
         'Иван'),
        ('v18_hyphenated_name',
         'NAME = "Mary-Jane"\nprobe = SpeakerProbe(NAME, NAME)',
         'Mary-Jane'),
        ('v18_apostrophe_name',
         'NAME = "O\'Brien"\nprobe = SpeakerProbe(NAME, NAME)',
         "O'Brien"),
        ('v18_full_name_two_words',
         'NAME = "Alice Smith"\nprobe = SpeakerProbe(NAME, NAME)',
         'Alice Smith'),
        ('v18_bytes_literal',
         'NAME = b"Alice".decode()\nprobe = SpeakerProbe(NAME, NAME)',
         'Alice'),
        ('v18_transcript_address_after_greeting',
         'transcript = "Hey Alice, turn on the lights"',
         'Alice'),
        ('v18_transcript_address_first',
         'transcript = "Alice, turn on the lights"',
         'Alice'),
        ('v19_full_name_with_initial',
         'NAME = "Alice B. Smith"\nprobe = SpeakerProbe(NAME, NAME)',
         'Alice B. Smith'),
        ('v19_initial_and_surname',
         'NAME = "A. Smith"\nprobe = SpeakerProbe(NAME, NAME)',
         'A. Smith'),
        ('v19_triple_hyphenated',
         'NAME = "Jean-Pierre-Marie"\nprobe = SpeakerProbe(NAME, NAME)',
         'Jean-Pierre-Marie'),
        ('v19_three_word_full_name',
         'NAME = "Mary Jane Watson"\nprobe = SpeakerProbe(NAME, NAME)',
         'Mary Jane Watson'),
    )

    def test_every_reviewed_bypass_is_closed(self):
        self.assertNotIn("alice", SPEAKER_FIXTURE_NAMES)
        for label, source, planted in self.REVIEWED_BYPASSES:
            with self.subTest(bypass=label):
                self.assertIn(planted, self._offenders(source))

    def test_a_planted_off_pool_name_fails_and_a_pool_name_passes(self):
        """The gate can fail, through the indirection a slot scan cannot see."""
        planted = 'NAME = "Ethel"\nprobe = SpeakerProbe(NAME, NAME)'
        self.assertEqual(self._offenders(planted), ["Ethel"])
        self.assertEqual(self._offenders(planted.replace("Ethel", "Sam")), [])

    def test_a_pool_only_full_name_is_not_an_offender(self):
        self.assertEqual(self._offenders('NAME = "Sam Morgan"'), [])
        self.assertEqual(self._offenders('NAME = "Sam B. Morgan"'), [])
        self.assertEqual(self._offenders('NAME = "Sam Smith"'), ["Sam Smith"])

    # Shapes that must stay clean, so the literal rule is known to be narrow
    # rather than a scan that reports every string it sees.
    NOT_NAME_SHAPED = (
        ("lowercase", 'tool = "alice"'),
        ("all_capitals", 'label = "ALICE"'),
        ("camel_case_identifier", 'intent = "HassTurnOn"'),
        ("sentence_initial_word", 'phrase = "Adding cookies to the list"'),
        ("single_letter", 'initial = "A"'),
        ("word_with_digits", 'model = "Gpt4"'),
        # The widening removed a word cap, so narrowness has to be re-proved
        # at phrase length: a title-case sentence fragment and a bare initial
        # must still read clean.
        ("a_bare_initial", 'initial = "A."'),
        ("two_initials_are_not_a_phrase", 'initials = "A"'),
        ("lowercase_particle", 'author = "van der Berg"'),
        ("postnominal_with_an_internal_capital", 'byline = "Alice Smith PhD"'),
    )

    def test_the_literal_rule_is_narrow(self):
        for label, source in self.NOT_NAME_SHAPED:
            with self.subTest(form=label):
                self.assertEqual(name_shaped_literals(source), set())

    # One source per fixture form the slot scan reads, each with an off-pool
    # name, and the forms it must not read.
    ACCEPTED_FORMS = (
        ("probe_positional", "probe = SpeakerProbe('Ethel', 'Mabel')",
         {"Ethel", "Mabel"}),
        ("probe_keyword",
         "probe = SpeakerProbe(male_name='Ethel', female_name='Mabel')",
         {"Ethel", "Mabel"}),
        ("probe_splat", "probe = SpeakerProbe(*('Ethel', 'Mabel'))",
         {"Ethel", "Mabel"}),
        ("voice_type_mapping", "names = {'male': 'Gertrude', 'female': 'Hilda'}",
         {"Gertrude", "Hilda"}),
        ("voice_type_mapping_with_extra_keys",
         "names = {'male': 'Gertrude', 'female': 'Hilda', 'guest': 'Nadia'}",
         {"Gertrude", "Hilda"}),
        ("slot_assignment", "probe.current_name = 'Ingrid'", {"Ingrid"}),
        ("slot_annotated_assignment", "probe.current_name: str = 'Ingrid'",
         {"Ingrid"}),
        ("slot_unpacked_assignment",
         "probe.male_name, probe.female_name = 'Ethel', 'Mabel'",
         {"Ethel", "Mabel"}),
        ("slot_walrus", "if (current_name := 'Ingrid'):\n    pass", {"Ingrid"}),
        ("phrase_positional", "phrase = progress_phrase('tool', {}, 'Jolene')",
         {"Jolene"}),
        ("phrase_speaker_name_keyword",
         "phrase = progress_phrase(tool_name='web_search', arguments={},\n"
         "                         speaker_name='Jolene')",
         {"Jolene"}),
        ("phrase_mapping_splat",
         "phrase = progress_phrase(**{'speaker_name': 'Jolene'})", {"Jolene"}),
        ("phrase_splat", "phrase = progress_phrase(*('tool', {}, 'Jolene'))",
         {"Jolene", "tool"}),
        ("personalized_phrase",
         "expected = 'Adding cookies to the shopping list, Katarzyna.'",
         {"Katarzyna"}),
    )

    REJECTED_FORMS = (
        ("non_name_mapping_value", "unrelated = {'male': 1, 'other': 'Leopold'}"),
        ("unknown_call", "tool_call = some_other_call('tool', {}, 'Mortimer')"),
        ("probe_from_variables", "probe = SpeakerProbe(male, female)"),
        ("bare_annotation_has_no_value", "probe.current_name: str"),
        ("phrase_tool_and_arguments_are_not_names",
         "phrase = progress_phrase('HassListAddItem', {'name': 'Mortimer'})"),
    )

    def test_the_speaker_slot_scan_reads_each_fixture_form(self):
        for label, source, expected in self.ACCEPTED_FORMS:
            with self.subTest(form=label):
                self.assertEqual(speaker_names_in(source), expected)

    def test_the_speaker_slot_scan_is_narrow(self):
        for label, source in self.REJECTED_FORMS:
            with self.subTest(form=label):
                self.assertEqual(speaker_names_in(source), set())

    def test_the_pool_is_actually_used(self):
        """A pool nothing draws from would make the gate vacuous."""
        used = set()
        for rel in self.sources():
            used |= {name.lower() for name in speaker_names_in(
                (REPOSITORY_ROOT / rel).read_text(encoding="utf-8"))}
        self.assertTrue(used, "no speaker fixture found at all; the scan has "
                              "stopped reading the shapes it is meant to read")
        self.assertTrue(used <= SPEAKER_FIXTURE_NAMES)


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
