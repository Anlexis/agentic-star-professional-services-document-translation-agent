"""SVC-C2-008 — Unit tests: caller trust gate.

Every invocation here goes through `node(state)` — BaseNode.__call__ — which runs
the trust gate -> input gate -> execute() -> output gate chain. Calling
`node.execute(state)` directly skips __call__ entirely, so the trust gate never
runs and a "trust" test written that way asserts nothing.

Contract exercised:
  - A denial RETURNS an error dict (it never raises): status ERROR plus
    "trust gate denied" in error_log.
  - execute() does not run on a denial, so every key that only execute() writes is
    ABSENT from the returned dict. The assertions below are keyed on the output of
    the node under test specifically (ValidateInputNode writes validated_input;
    SecurityGateOutputNode writes its own "SecurityGateOutputNode: <field> failed ..."
    error and formatted_output), not on a bare status check that some other gate in
    the pipeline could satisfy.
"""

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_document_type_node import ClassifyDocumentTypeNode
from src.nodes.detect_source_language_node import DetectSourceLanguageNode
from src.nodes.security_gate_output_node import (
    _OUTPUT_BEARING_FIELDS,
    _SCREENED_FIELD,
    _WITHHELD_NOTICE,
    SecurityGateOutputNode,
)
from src.nodes.translate_with_domain_terms_node import TranslateWithDomainTermsNode
from src.nodes.validate_input_node import ValidateInputNode

DOCUMENT = "This agreement sets out the terms and conditions between the parties."


@pytest.fixture(autouse=True)
def _patch_emit(monkeypatch):
    """Silence emit_trace_event in the node modules under test (never via sys.modules)."""

    def noop(*a, **kw):
        return None

    for mod in (
        "src.nodes.validate_input_node",
        "src.nodes.detect_source_language_node",
        "src.nodes.security_gate_output_node",
    ):
        monkeypatch.setattr(mod + ".emit_trace_event", noop, raising=False)


def _state(trust_value: str, **extra) -> dict:
    """Outer-graph state carrying an explicit caller trust level."""
    state = {
        "user_input": DOCUMENT,
        "caller_trust_level": trust_value,
        "node_history": [],
        "error_log": [],
        "session_id": "test-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestTrustGate:
    """Trust enforcement on the VERIFIED_EXTERNAL pre_process slot."""

    def test_anonymous_caller_denied_on_validate_input(self):
        """ANONYMOUS caller on the VERIFIED_EXTERNAL ValidateInputNode is denied.

        __call__ must RETURN the error dict rather than raise, and
        ValidateInputNode's own output must not appear — execute() never ran.
        """
        node = ValidateInputNode()  # required_trust_level = VERIFIED_EXTERNAL
        result = node(_state(TrustLevel.ANONYMOUS.value))

        assert result.get("status") == AgentStatus.ERROR.value
        error_log = result.get("error_log", [])
        assert any(
            "trust gate denied" in str(e) for e in error_log
        ), f"Expected 'trust gate denied' in error_log, got: {error_log}"
        assert "validated_input" not in result, "trust denial leaked validated_input"

    def test_verified_external_caller_passes_validate_input(self):
        """VERIFIED_EXTERNAL caller clears the gate and ValidateInputNode runs.

        Positive control for the denial test above: same node, same document,
        differing only in caller trust, produces this node's own validated output.
        """
        node = ValidateInputNode()
        result = node(_state(TrustLevel.VERIFIED_EXTERNAL.value))

        assert result.get("status") == AgentStatus.SUCCESS.value
        assert result.get("validated_input") == DOCUMENT

    def test_gate_runs_before_node_input_validation(self):
        """An ANONYMOUS caller is refused before ValidateInputNode's own checks.

        The payload here carries a credential pattern that the node itself blocks,
        so a "trust gate denied" verdict — with no credential finding — proves the
        framework gate fired first and execute() never ran.
        """
        node = ValidateInputNode()
        payload = "Please translate this: sk-" + "A" * 30
        result = node(_state(TrustLevel.ANONYMOUS.value, user_input=payload))

        assert result.get("status") == AgentStatus.ERROR.value
        assert any("trust gate denied" in str(e) for e in result.get("error_log", []))
        assert not any(
            "credential" in str(e).lower() for e in result.get("error_log", [])
        ), "execute() ran despite the trust denial"

    def test_verified_external_caller_rejected_on_empty_document(self):
        """Gate passes, then ValidateInputNode's own validation declines empty input."""
        node = ValidateInputNode()
        result = node(_state(TrustLevel.VERIFIED_EXTERNAL.value, user_input="   "))
        assert result.get("status") == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can correct the value and send the request again.
        assert result.get("error_code")
        assert any("empty" in str(e) for e in result.get("error_log", []))
        assert not any(
            "trust gate denied" in str(e) for e in result.get("error_log", [])
        ), "a VERIFIED_EXTERNAL caller must clear the trust gate"

    def test_anonymous_caller_allowed_on_inner_node(self):
        """An ANONYMOUS inner domain node admits an ANONYMOUS caller and runs."""
        node = DetectSourceLanguageNode()  # required_trust_level = ANONYMOUS
        result = node(_state(TrustLevel.ANONYMOUS.value, validated_input=DOCUMENT))

        assert result.get("status") == AgentStatus.SUCCESS.value
        assert result.get("source_language") == "EN"
        assert result.get("target_language") == "JA"
        assert not any("trust gate denied" in str(e) for e in result.get("error_log", []))

    def test_anonymous_caller_reaches_output_gate_own_verdict(self):
        """The post_process node is ANONYMOUS, so its own output gate is what decides.

        A credential pattern in translated_output must be blocked by
        SecurityGateOutputNode's module-level output gate, identifiable by that
        node's own error prefix — not by a trust denial and not by a framework
        backstop.
        """
        node = SecurityGateOutputNode()
        leaked = "[EN-JA] The key is sk-" + "A" * 30
        result = node(
            _state(
                TrustLevel.ANONYMOUS.value,
                translated_output=leaked,
                document_type="contract",
                source_language="EN",
                target_language="JA",
            )
        )

        assert result.get("status") == AgentStatus.ERROR.value
        error_log = result.get("error_log", [])
        assert any(
            str(e) == f"SecurityGateOutputNode: {_SCREENED_FIELD} failed the output gate" for e in error_log
        ), f"Expected this node's own output-gate verdict, got: {error_log}"
        assert not any(
            "trust gate denied" in str(e) for e in error_log
        ), "ANONYMOUS is admitted here — the trust gate must not be what blocked it"

        # Withholding, not merely erroring. This assertion used to read
        # `"formatted_output" not in result`, which passes on a node that
        # withholds nothing: the base envelope resolves `formatted_output or
        # result`, so an absent (or empty) formatted_output falls through to
        # `result` — the un-screened translation the gate just refused. The
        # notice has to be PRESENT and TRUTHY to short-circuit that fallback.
        assert result.get("formatted_output") == _WITHHELD_NOTICE
        assert bool(result.get("formatted_output")), "a falsy notice re-opens the `or result` fallback"
        assert leaked not in str(result), "the refused translation must not survive anywhere in the delta"
        for field in _OUTPUT_BEARING_FIELDS:
            assert result.get(field) is None, f"{field} still carries the refused translation"

    def test_anonymous_caller_passes_clean_output_gate(self):
        """Clean output clears the output gate and the node emits formatted_output."""
        node = SecurityGateOutputNode()
        clean = "[EN-JA] The contract is executed."
        result = node(
            _state(
                TrustLevel.ANONYMOUS.value,
                translated_output=clean,
                document_type="contract",
                source_language="EN",
                target_language="JA",
            )
        )
        assert result.get("status") == AgentStatus.SUCCESS.value
        assert result.get("formatted_output") == clean


class TestTrustLevelMatrix:
    """The template's declared trust matrix (docs/02_design.md).

    The outer pre_process slot is the external boundary and requires
    VERIFIED_EXTERNAL; the post_process gate and the three inner domain nodes run
    behind that boundary and are declared ANONYMOUS per the Cat-2 nested convention.
    """

    def test_pre_process_slot_requires_verified_external(self):
        assert ValidateInputNode.required_trust_level is TrustLevel.VERIFIED_EXTERNAL

    def test_inner_and_output_nodes_admit_anonymous(self):
        for node_cls in (
            SecurityGateOutputNode,
            DetectSourceLanguageNode,
            ClassifyDocumentTypeNode,
            TranslateWithDomainTermsNode,
        ):
            assert node_cls.required_trust_level is TrustLevel.ANONYMOUS, (
                f"{node_cls.__name__} must declare TrustLevel.ANONYMOUS "
                "(inner Cat-2 domain node / post-boundary output gate)"
            )
