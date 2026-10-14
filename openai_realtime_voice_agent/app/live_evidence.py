"""Session/utterance scoped backend evidence; not a success-language classifier.

A submitted tool outcome (including a refusal/clarification) permits a reply
only after its continuation completes. Arbitrary backend prose never proves
an action happened. No-tool structured refusals are a separate speech class.

Continuation ownership is deliberately tag-agnostic. ``response.create`` has
no field that names the delegation it continues, so the endpoint is free to
answer one either tagged with the delegation or untagged, and which it does is
not under this client's control. Keying ownership on the *caller's* knowledge
of a delegation id while keying acceptance on the *server's* tag is what let a
successful tool call end with no final reply: a delegated continuation that
came back untagged was rejected outright, ``pending_continuations`` never
decremented, and ``ready`` could never become True again for that turn
(retained Workshop turn c0ffee02-17). Every create this client issues is
therefore registered once, and the first new response of the turn claims it
whichever way the endpoint labelled it.

Single-flight is scoped to the current turn rather than the whole session for
the same reason. Retired requests are still kept so a late create consumes its
original owner and is rejected instead of being mistaken for the next
utterance's answer — but a retired request must not be able to refuse every
future create, which previously disabled both the silent-tool rescue and every
client continuation for the rest of a session (retained Kitchen turn
c0ffee01-18).
"""
import re

# Retired create requests kept for late-event correlation. Only the newest few
# can still be claimed in practice; the bound exists so a long session cannot
# grow this without limit.
MAX_TRACKED_REQUESTS = 32

# How many superseded turns an unclaimed create keeps vetoing a new one for.
# 1 covers the real ambiguity — a create issued for the turn a new utterance
# just superseded can still be answered — without letting one unanswered
# create silence the rest of the session.
RETIRED_REQUEST_VETO_TURNS = 1

# Tool-name tokens that denote changing the house, and tokens that denote
# reading it. Matched as whole tokens, never as substrings: "ask_openclaw"
# contains "open", and substring matching therefore classified an external
# agent lookup as an ACTION — which would have let "open the garage" be
# satisfied by an ask_openclaw result that opened nothing.
_ACTION_TOKENS = frozenset({
    "turn", "set", "add", "remove", "delete", "complete", "lock", "unlock",
    "open", "close", "play", "pause", "unpause", "start", "stop", "cancel",
    "dim", "brighten", "confirm", "mute", "unmute", "broadcast", "position",
    "volume", "clean", "vacuum", "remember", "forget", "send", "move",
    "moving", "temperature", "speed", "next", "previous", "return",
    # Imperative state words. A read-only tool is not named "…On"/"…Off",
    # and this is what classifies an installation's own intent scripts
    # (``car_climate_on``, ``CarClimateOn``) as the actions they are.
    "on", "off", "arm", "disarm", "watch", "disconnect",
})
_QUERY_TOKENS = frozenset({
    "get", "list", "find", "search", "check", "status", "state", "states",
    "read", "recall", "lookup", "query", "items", "ask", "context",
    "eta", "answer",
})
# Separators, plus the two camel-case boundaries: lower/digit -> upper
# ("ListAdd") and the end of an acronym ("HassTurnOn", "PersonETA").
_TOKEN_SPLIT = re.compile(
    r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
)


def tool_name_tokens(name):
    """Lowercase whole tokens of a tool name, ignoring any MCP namespace."""
    bare = (name or "").split("__")[-1]
    return {t.lower() for t in _TOKEN_SPLIT.split(bare) if t}


def classify_tool_name(name):
    """``'clock'``, ``'action'``, ``'query'`` or ``''`` for one tool name.

    An action token wins over a query token: ``HassMediaSearchAndPlay`` reads
    like a search and then changes the house, and the stricter kind is the
    honest one. An unrecognised name is left unclassified rather than guessed.
    """
    bare = (name or "").split("__")[-1]
    tokens = tool_name_tokens(bare)
    if bare.lower() == "getdatetime" or tokens >= {"get", "date", "time"}:
        return "clock"
    if tokens & _ACTION_TOKENS:
        return "action"
    if tokens & _QUERY_TOKENS:
        return "query"
    return ""


class TurnEvidence:
    def __init__(self):
        self.generation = 0
        self.delegations = {}
        self.responses = {}
        self.calls = {}
        self.call_names = {}
        self.finished = set()
        self.client_requests = []
        self.next_turn()

    def next_turn(self):
        self.generation += 1
        self.tool_results = set()
        self.result_kinds = set()
        self.unclassified_results = set()
        self.requirement = None
        self.pending_continuations = 0
        self.ready = False
        self.kind = None

    def delegate(self, did):
        if did in self.delegations:
            return False
        self.delegations[did] = self.generation
        self.ready = False
        return True

    def request_client_response(self, event_id, delegation_id=None):
        """Single-flight ownership for one client-issued ``response.create``.

        ``delegation_id`` is what this client *believes* it is continuing, or
        None. It is only a hint for correlation: the endpoint may answer either
        way, so the request is claimable by a tagged or an untagged response.

        Refused while an *ambiguous* request is still outstanding: one naming
        the same delegation, or one where either side is unknown and the two
        answers could not be told apart. Two concurrent delegations with
        distinct ids each get their own create.

        The veto is bounded to the current turn and the one it superseded,
        which is as long as a late create can plausibly still arrive. That
        bound is the fix for the session-wide latch: an entry that was never
        claimed used to refuse every future create for the life of the
        session, which disabled both the silent-tool rescue and every client
        continuation (retained Kitchen turn c0ffee01-18). A create arriving
        after its owner has aged out finds nothing to claim and is rejected by
        ``accept`` instead, so the fail-closed protection is unchanged — the
        entry is retained for correlation, it simply stops holding a veto.
        """
        for _, generation, expected in self.client_requests:
            if self.generation - generation > RETIRED_REQUEST_VETO_TURNS:
                continue
            if expected is None or delegation_id is None or expected == delegation_id:
                return False
        self.client_requests.append((event_id, self.generation, delegation_id))
        del self.client_requests[:-MAX_TRACKED_REQUESTS]
        self.ready = False
        return True

    def has_outstanding_request(self):
        """Is a create this turn issued still waiting for its response?

        What a caller actually needs to know before issuing another one.
        Testing ``client_requests`` for emptiness instead treated a retained,
        aged-out correlation entry as live work, which permanently blocked the
        silent-tool rescue for the rest of a session.
        """
        return any(self.generation - generation <= RETIRED_REQUEST_VETO_TURNS
                   for _, generation, _ in self.client_requests)

    def _claim_request(self, delegation_id):
        """Consume the outstanding create this response can belong to.

        An exact delegation match wins, so two concurrent tagged continuations
        resolve to their own slots. Otherwise the oldest claimable slot is
        taken, which means a late create from a superseded turn consumes its
        own retired owner and is rejected rather than being mistaken for the
        current turn's answer.
        """
        for exact in (True, False):
            for index, (_, generation, expected) in enumerate(self.client_requests):
                if exact:
                    if not delegation_id or expected != delegation_id:
                        continue
                elif not (expected is None or not delegation_id):
                    continue
                del self.client_requests[index]
                return generation
        return None

    def requirement_met(self):
        """Does the evidence answer what the user's words demanded?

        ``action`` and ``clock`` stay strict: a tool that changes the house, or
        the clock itself, must have been called and returned, because the voice
        model must never be able to claim either from prose. ``query`` is
        satisfied by any returned tool result — the requirement exists to prove
        the answer came from a tool instead of being invented, and the tool's
        *name* carries no safety information about that. Demanding a
        query-shaped name instead is what made `web_search` unsatisfiable and
        left a successful lookup with no admissible reply.
        """
        if self.requirement is None:
            return True
        if self.requirement in self.result_kinds:
            return True
        if self.requirement == "query":
            return bool(self.tool_results)
        return False

    def accept(self, evt):
        inner = evt.get('event') or {}
        did = str(evt.get('delegation_id') or '')
        rid = inner.get('response_id') or (inner.get('response') or {}).get('id')
        kind = inner.get('type')
        if not did:
            if kind == 'response.created':
                if rid is None or ('', rid) in self.responses:
                    return False
                owner = self._claim_request(None)
                if owner is None:
                    return False
                if owner != self.generation:
                    self.responses[('', rid)] = owner
                    self.finished.add(('', rid))
                    return False
            elif not any(d == '' and g == self.generation and (d, r) not in self.finished
                         for (d, r), g in self.responses.items()):
                return False
        elif self.delegations.get(did) != self.generation:
            return False
        elif kind == 'response.created':
            # A continuation this client issued may come back tagged with the
            # delegation it continues instead of untagged. Release its request
            # slot either way, so one unlucky labelling cannot latch the turn.
            self._claim_request(did)
        if rid is None:
            rid = next((r for (d, r), g in reversed(list(self.responses.items()))
                        if d == did and g == self.generation and (d, r) not in self.finished), None)
        key = (did, rid)
        if rid is None and kind in ('response.failed', 'response.incomplete'):
            # A rejected create can fail before a response id exists. Surface
            # its error once, but it can never confer successful-turn evidence.
            key = (did, 'uncreated-failure')
            if key in self.finished:
                return False
            self.finished.add(key)
            self.ready = False
            return True
        if rid is None or key in self.finished:
            return False
        if kind == 'response.created':
            if key in self.responses:
                return False
            self.responses[key] = self.generation
            if self.pending_continuations:
                self.pending_continuations -= 1
            self.ready = False
        elif self.responses.get(key) != self.generation:
            return False
        if kind == 'response.output_item.done':
            item = inner.get('item') or {}
            if item.get('type') == 'function_call':
                self.calls[item.get('call_id')] = self.generation
                self.call_names[item.get('call_id')] = item.get('name', '')
                self.ready = False
        if kind in ('response.completed', 'response.failed', 'response.incomplete'):
            self.finished.add(key)
            self.ready = False
            if kind == 'response.completed':
                self.kind = None
                content = [c for item in (inner.get('response') or {}).get('output', [])
                           for c in item.get('content', [])]
                refusal = bool(content) and all(c.get('type') == 'refusal' for c in content)
                text = ''.join(c.get('text', '') for c in content if c.get('type') == 'output_text')
                clarification = bool(re.fullmatch(
                    r"(?:Which|What|Could you specify|Can you specify) [^.!?\n]{1,200}\?", text))
                if refusal:
                    self.kind = 'refusal'
                elif clarification:
                    self.kind = 'clarification'
                elif self.tool_results and self.requirement_met():
                    self.kind = 'tool_result'
                active = any(g == self.generation and k not in self.finished
                             for k, g in self.responses.items())
                self.ready = bool(self.kind) and not active and not self.pending_continuations
        return True

    def result(self, call_id):
        if self.calls.get(call_id) != self.generation:
            return False
        self.tool_results.add(call_id)
        name = self.call_names.get(call_id, '')
        kind = classify_tool_name(name)
        if kind:
            self.result_kinds.add(kind)
        else:
            # Kept so a turn that cannot satisfy its requirement is
            # diagnosable instead of merely stuck.
            self.unclassified_results.add(name.split('__')[-1])
        self.ready = False
        return True

    def continuation(self):
        self.pending_continuations += 1
        self.ready = False
