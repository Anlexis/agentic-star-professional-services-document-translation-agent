# PB — output-gate CONTAINMENT, end to end through the real ASGI /invoke.
#
# Refusing is not containing. By the time the output gate runs, the main slot
# has already mapped the translated document into the OUTER state:
# TranslationWorkflowGraphNode.merge_output() writes it to both
# `translated_output` and `result`. The base envelope then resolves the
# released text as
#     formatted_output or result
# so a gate that returns ERROR without touching those fields still SHIPS the
# document it just refused — and for this template that document is the
# customer's translated contract.
#
# The distinction that makes this subtle is falsy-vs-truthy: writing "" to
# formatted_output LOOKS like clearing it and is not, because `"" or result` is
# `result`. Only a non-empty notice short-circuits the fallback. Both halves are
# pinned here, at the surface a caller actually reaches.
#
# The blocked path is driven through the domain's own behaviour rather than a
# patched screen: a caller glossary that expands a short recurring term into a
# long one pushes the TRANSLATED document past the gate's size ceiling while the
# submitted document stays inside its own bound. Nothing is monkeypatched, so
# what these tests exercise is the shipped pipeline.
#
# docs/03_test_spec.md §9. Deterministic — no network beyond the in-process
# transport, no model call.

import importlib
import json
import warnings

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # httpx packaging note, not a template behaviour
    from fastapi.testclient import TestClient

from framework.schemas.agent_status import AgentStatus
from src.graph.graph import TranslationWorkflowGraphNode
from src.nodes.security_gate_output_node import (
    _MAX_OUTPUT_CHARS,
    _OUTPUT_BEARING_FIELDS,
    _WITHHELD_NOTICE,
    SecurityGateOutputNode,
)

_TOKEN = "pb-containment-token"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}

# Confidential content that must never appear in a refused envelope. It is
# ordinary document text, not a credential: a credential would be stopped by
# the framework's own output scan upstream, and the test would then prove
# nothing about THIS template's gate.
_MARKER = "CONFIDENTIAL ACQUISITION PRICE 4.2 BILLION YEN"

# 80 characters — the caller-glossary entry ceiling. Substituted for each
# occurrence of a 4-character term, this multiplies the document ~20x.
_LONG_TRANSLATION = "kokusai zaimu hokoku kijun no teiyaku joko ni motozuku kaishaku shishin" + "." * 9
_TERM = "IFRS"

# Sized so the submitted document stays under the input bound while the
# translated one clears the gate ceiling with room to spare.
_OCCURRENCES = 19_000
_LEAKING_DOCUMENT = _MARKER + " " + f"{_TERM} " * _OCCURRENCES
_CLEAN_DOCUMENT = (
    f"{_MARKER} This engagement contract sets out the terms between the parties, "
    f"including the {_TERM} reporting basis and the agreed liability limits."
)

# What the pipeline hands the gate: the submitted document after glossary
# substitution and direction tagging. The node-level tests below screen THIS,
# not the submitted form — the submitted document is under the ceiling, and
# feeding it to the gate would exercise the pass path while appearing to test
# the block path.
_TRANSLATED_LEAKING = "[EN→JA] " + _MARKER + " " + f"{_LONG_TRANSLATION} " * _OCCURRENCES


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
    server = importlib.reload(importlib.import_module("src.api.server"))
    return TestClient(server.app)


def _translate(client, document):
    body = {
        "input": "translate this engagement contract",
        "input_context": {
            "document": document,
            "document_type": "contract",
            "target_language": "ja",
            "glossary_terms": {_TERM: _LONG_TRANSLATION},
        },
    }
    response = client.post(
        "/invoke",
        content=json.dumps(body).encode(),
        headers={**_HEADERS, "Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestTheFixtureActuallyReachesTheGate:
    """Guard the premise. If the document stopped being oversized after
    translation, or stopped reaching the gate at all, the containment tests
    below would pass while proving nothing."""

    def test_submitted_document_is_within_its_own_bound(self):
        from src.nodes.validate_input_node import _MAX_DOCUMENT_CHARS

        assert len(_LEAKING_DOCUMENT) < _MAX_DOCUMENT_CHARS

    def test_translated_document_clears_the_gate_ceiling(self):
        assert len(_TRANSLATED_LEAKING) > _MAX_OUTPUT_CHARS

    def test_the_gate_node_runs_on_the_blocked_invoke(self, client):
        envelope = _translate(client, _LEAKING_DOCUMENT)
        assert "SecurityGateOutputNode" in envelope["node_history"]


class TestBlockedTranslationIsContained:
    def test_error_envelope_carries_no_translated_document(self, client):
        envelope = _translate(client, _LEAKING_DOCUMENT)
        output = envelope["output"] or ""
        assert envelope["status"] == AgentStatus.ERROR.value
        assert _MARKER not in output
        assert _LONG_TRANSLATION not in output
        assert output == _WITHHELD_NOTICE

    def test_error_envelope_leaks_no_diagnostics(self, client):
        """The refusal names the screened field — not the matched text, not the
        blocked form, and nothing about the machine it ran on."""
        envelope = _translate(client, _LEAKING_DOCUMENT)
        surfaced = json.dumps(envelope, default=str)
        assert _MARKER not in surfaced
        assert "Traceback" not in surfaced
        assert ".py" not in surfaced
        assert "/Users/" not in surfaced and "/builds/" not in surfaced

    def test_an_empty_formatted_output_would_have_released_the_document(self):
        """The falsy/truthy distinction, measured rather than assumed.

        This is the whole defect in one test: `""` reads like a cleared field,
        but `"" or result` is `result`. A future tidy-up that empties the notice
        silently re-opens the leak, so the truthiness is pinned here.
        """
        from src.graph.graph import ProfServicesTranslationAgent

        agent = ProfServicesTranslationAgent()
        released = {"result": _TRANSLATED_LEAKING, "translated_output": _TRANSLATED_LEAKING}

        emptied = {**released, "formatted_output": "", "status": AgentStatus.ERROR.value}
        assert (
            agent.get_output(emptied)["output"] == _TRANSLATED_LEAKING
        ), "premise check: an empty formatted_output falls through to `result`"

        contained = {**released, **SecurityGateOutputNode().execute(released)}
        assert bool(_WITHHELD_NOTICE) is True
        assert agent.get_output(contained)["output"] == _WITHHELD_NOTICE

    def test_every_document_bearing_field_is_cleared(self):
        state = {"result": _TRANSLATED_LEAKING, "translated_output": _TRANSLATED_LEAKING}
        state.update(SecurityGateOutputNode().execute(state))
        for field in _OUTPUT_BEARING_FIELDS:
            assert state[field] is None, f"{field} still holds the refused translation"

    def test_clearing_covers_everything_the_main_slot_maps(self):
        """Inventory guard: a field added to merge_output() must be either
        cleared by the gate or declared content-free here, never neither."""
        # error_code is content-free by the same rule as status: it is a closed
        # set of reason codes, never caller content or document text. It must
        # stay OUT of the cleared set — the gate's refusal clears the fields
        # that carry the document, and clearing the reason with them would
        # leave the caller a blank body with nothing saying why.
        content_free = {"status", "error_code", "document_type", "source_language", "target_language"}
        mapped = set(TranslationWorkflowGraphNode().merge_output({}, {}))
        unaccounted = mapped - set(_OUTPUT_BEARING_FIELDS) - content_free
        assert not unaccounted, f"merge_output maps {sorted(unaccounted)}; the gate neither clears nor exempts it"


class TestCleanTranslationStillShips:
    """Control. Containment that also withholds legitimate translations is not
    a fix — same route, same glossary, same gate, only the size violation gone."""

    def test_a_legitimate_translation_reaches_the_caller_in_full(self, client):
        envelope = _translate(client, _CLEAN_DOCUMENT)
        output = envelope["output"] or ""
        assert envelope["status"] == AgentStatus.SUCCESS.value
        assert output != _WITHHELD_NOTICE
        assert _MARKER in output, "the customer's document must still be translated and returned"
        assert _LONG_TRANSLATION in output, "the caller's glossary term must still be applied"
        assert "SecurityGateOutputNode" in envelope["node_history"]
