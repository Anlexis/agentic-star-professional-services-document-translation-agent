"""AgentCore Platform v1.0"""

# Inner domain workflow graph — Cat 2 multi-step translation pipeline.
#
# Called by TranslationWorkflowGraphNode.get_subgraph() in graph.py.
# Inherits BaseGraph (fully custom node topology — no forced backbone).
#
# Pipeline (linear):
#   START → detect_source_language → classify_document_type
#         → translate_with_domain_terms → END
#
# All 7 BaseGraph abstract methods are implemented:
#   name, state_schema, _validate_config, register_nodes,
#   add_edges, route, get_output.
#
# Node instantiation: NO constructor arguments (nodes are no-arg by contract).
# Inner nodes declare TrustLevel.ANONYMOUS — the sub-graph inherits the outer
# InvocationContext unchanged (no trust escalation); the external gate stays on
# the outer backbone ValidateInputNode (pre_process slot, VERIFIED_EXTERNAL).

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_input_context
from src.nodes.classify_document_type_node import ClassifyDocumentTypeNode
from src.nodes.detect_source_language_node import DetectSourceLanguageNode
from src.nodes.translate_with_domain_terms_node import TranslateWithDomainTermsNode
from src.schemas.state import State, to_json


class DomainWorkflowGraph(BaseGraph):
    """Inner graph: 3-step professional services document translation workflow.

    Instantiated by TranslationWorkflowGraphNode.get_subgraph(), which passes
    the validated runtime settings from _parent_config().
    get_output() is designed together with TranslationWorkflowGraphNode.merge_output()
    in graph.py — the field names here and in merge_output() must be consistent.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "ProfServicesTranslationWorkflow"

    @property
    def state_schema(self) -> type:
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """No mandatory runtime config for the translation inner graph.

        The forwarded settings block is read with safe defaults by the domain
        nodes, so its absence is non-fatal — validation is permissive here
        rather than raising ConfigError.
        """
        pass

    # ── Boundary seeding (settings + bridged caller data -> inner state) ──────

    def _extra_initial_state(self) -> dict[str, Any]:
        """Seed the inner initial state from the forwarded settings and bridge.

        TranslationWorkflowGraphNode._parent_config() forwards the validated
        runtime settings under config["configurable"]["settings"]; this hook
        makes that block reachable by the domain nodes at runtime as the
        JSON-string state field `runtime_settings` (dict-valued state travels
        as a JSON string, never a bare dict).

        The validated document and caller request cross the graph boundary on
        the context bridge (src/graph/context_bridge.py): the framework does
        not forward outer state fields into a subgraph, and the `user_input`
        field it does forward is one the platform's input gate rewrites — so
        the document is republished here under `document_text`, which the gate
        leaves untouched.

        Passing values through state rather than a second execute() argument is
        what keeps every node at the canonical execute(self, state) -> dict.
        """
        configurable = (self.config or {}).get("configurable") or {}
        settings = configurable.get("settings") or {}
        bridged = get_caller_input_context()
        return {
            "runtime_settings": to_json(settings),
            "document_text": bridged.get("document_text") or "",
            "caller_request": bridged.get("caller_request") or "{}",
        }

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        All instantiated with NO constructor arguments.
        """
        self._nodes["detect_source_language"] = DetectSourceLanguageNode()
        self._nodes["classify_document_type"] = ClassifyDocumentTypeNode()
        self._nodes["translate_with_domain_terms"] = TranslateWithDomainTermsNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear translation pipeline."""
        self._sg.add_edge(START, "detect_source_language")
        self._sg.add_edge("detect_source_language", "classify_document_type")
        self._sg.add_edge("classify_document_type", "translate_with_domain_terms")
        self._sg.add_edge("translate_with_domain_terms", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: AgentState) -> str:
        """Conditional routing (required by BaseGraph ABC).

        Linear topology — this method is never called unless
        add_conditional_edges() is added in a future extension.
        """
        return END if state.get("status") == AgentStatus.ERROR.value else END

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Shape the sub_result dict returned to TranslationWorkflowGraphNode.merge_output().

        Field names here MUST match what merge_output() reads:
          translated_output, document_type, source_language, target_language,
          output (mapped to `result` in the outer state), status.
        """
        return {
            # the reason must leave the subgraph or the outer graph cannot report it
            "error_code": state.get("error_code"),
            "output": state.get("translated_output") or state.get("formatted_output"),
            "translated_output": state.get("translated_output"),
            "document_type": state.get("document_type"),
            "source_language": state.get("source_language"),
            "target_language": state.get("target_language"),
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
