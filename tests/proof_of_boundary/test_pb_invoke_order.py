# PB-6: Invoke Execution Order Verification
# Verifies BaseNode.__call__() enforces: trust gate -> node_start ->
# _security_gate_input() -> execute() -> _security_gate_output() ->
# node_complete, for every concrete node under src/nodes/.
#
# PB-6b: Full-graph backbone slot order — ProfServicesTranslationAgent.invoke()
# must visit initialize → pre_process → main → post_process → finalize and
# return AgentStatus.SUCCESS for a valid English contract document.

import importlib
import inspect
import pkgutil

import pytest

# ---------------------------------------------------------------------------
# Constants used by PB-6b (graph-level test)
# ---------------------------------------------------------------------------

# The GraphNode subclass placed in the outer graph's "main" backbone slot.
# Must match the class name in src/graph/graph.py.
_MAIN_SLOT_NODE = "TranslationWorkflowGraphNode"

# A valid English contract document that:
#   - passes input validation (non-empty, < 100k chars, no credential patterns)
#   - is detected as EN by the CJK heuristic
#   - classifies as "contract" via keyword scoring
#   - produces an output that passes the output-gate size and credential checks
_VALID_PAYLOAD = (
    "This contract agreement sets out the terms and conditions between the parties, "
    "including indemnification clauses, liability limits, confidentiality obligations, "
    "and the governing law, subject to the jurisdiction agreed upon by all parties."
)


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ and collect concrete BaseNode subclasses."""
    from framework.nodes.base_node import BaseNode

    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError:
        return []

    discovered = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                discovered.append(attr)
    return discovered


class TestInvokeOrder:
    """PB-6: __call__ must run trust gate -> node_start -> input gate -> execute() -> output gate -> node_complete."""

    def test_call_order_for_every_node(self, monkeypatch):
        node_classes = _discover_node_classes()
        if not node_classes:
            pytest.skip("no concrete BaseNode subclasses found under src/nodes/")

        import framework.nodes.base_node as base_node_module

        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, _payload, _state, _o=order: _o.append(f"event:{event_type}"),
            )

            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _o=order, _label=label, _orig=original):
                    _o.append(_label)
                    return _orig(self, arg)

                monkeypatch.setattr(node_cls, method_name, spy)

            instance = node_cls()
            state = {
                "caller_trust_level": node_cls.required_trust_level.value,
                "correlation_id": "pb6-invoke-order-test",
            }
            instance(state)

            expected = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
                "event:node_complete",
            ]
            if order != expected:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\n" f"expected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)


class TestGraphInvokeOrder:
    """PB-6b: Full graph invoke must visit every backbone slot in order and return SUCCESS.

    Verifies:
      - Graph compiles without error.
      - agent.invoke(_VALID_PAYLOAD) visits InitializeNode → ValidateInputNode →
        TranslationWorkflowGraphNode → SecurityGateOutputNode → FinalizeNode
        (by class name in node_history).
      - Final status is AgentStatus.SUCCESS.
      - The main backbone slot is exactly _MAIN_SLOT_NODE.
    """

    def test_backbone_node_history(self, monkeypatch):
        """PB-6b: graph.invoke() must produce SUCCESS with full backbone node_history."""
        # Patch emit_trace_event at domain node modules to silence audit
        # side-effects. Patches are at the MODULE level, never via sys.modules.
        monkeypatch.setattr(
            "src.nodes.translate_with_domain_terms_node.emit_trace_event",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            "src.nodes.security_gate_output_node.emit_trace_event",
            lambda *a, **k: None,
        )

        from framework.schemas.agent_status import AgentStatus
        from framework.schemas.invocation_context import InvocationContext
        from framework.schemas.trust_level import TrustLevel
        from src.graph.graph import ProfServicesTranslationAgent

        agent = ProfServicesTranslationAgent()
        agent.compile()

        ctx = InvocationContext(
            session_id="pb6b-graph-test",
            caller_id="pb6b-test",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        )
        result = agent.invoke(_VALID_PAYLOAD, ctx=ctx)

        node_history = result.get("node_history", [])

        # Every backbone class must appear — failure means a slot was skipped.
        expected_backbone = (
            "InitializeNode",
            "ValidateInputNode",  # pre_process slot
            _MAIN_SLOT_NODE,  # main slot = TranslationWorkflowGraphNode
            "SecurityGateOutputNode",  # post_process slot
            "FinalizeNode",
        )
        for cls_name in expected_backbone:
            assert cls_name in node_history, (
                f"Backbone class '{cls_name}' missing from node_history; " f"got: {node_history}"
            )

        # Order: each backbone node must appear after the previous one.
        indices = [node_history.index(n) for n in expected_backbone]
        assert indices == sorted(indices), f"Backbone nodes out of order in node_history; got: {node_history}"

        # Final status must be SUCCESS for a valid payload.
        status = result.get("status")
        assert (
            status == AgentStatus.SUCCESS or status == AgentStatus.SUCCESS.value
        ), f"Expected SUCCESS, got {status!r}; node_history: {node_history}"
