"""AgentCore Platform v1.0"""

# Cat 2 outer graph — Professional Services Document Translation Agent.
#
# Architecture: Cat 2 nested pattern.
#   Outer graph (this file): AgentBaseGraph with 5-node backbone.
#   Inner graph (domain_workflow_graph.py): BaseGraph with 3 domain nodes.
#
# Backbone wiring (fixed — do NOT override add_edges()):
#   START → initialize → pre_process → main → {route} → post_process → finalize → END
#
# Slot assignment:
#   pre_process  : ValidateInputNode   — trust gate + caller contract + document screen
#   main         : TranslationWorkflowGraphNode — delegates to inner DomainWorkflowGraph
#   post_process : SecurityGateOutputNode — output gate
#
# Inner graph (DomainWorkflowGraph) pipeline:
#   detect_source_language → classify_document_type → translate_with_domain_terms
#
# Base class: AgentBaseGraph (direct framework inheritance).
#
# Runtime configuration:
#   config/agent.yaml is the static registration manifest — identity only, no
#   tuning. config/config.yaml holds every runtime parameter. The registry
#   loads that file and passes it as Graph(config=...); the standalone server
#   does the same through _runtime_config(), so a registry-loaded agent and a
#   deployed one see identical settings. Every declared value is validated once
#   in declared_settings() and then travels to its consumer — there is no
#   second copy of the defaults on disk.

import math
from pathlib import Path
from typing import Any, ClassVar, Dict, Optional, cast

from framework.schemas.agent_status import AgentStatus
from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from src.graph.context_bridge import set_caller_input_context
from src.nodes.security_gate_output_node import SecurityGateOutputNode
from src.nodes.validate_input_node import ValidateInputNode
from src.schemas.state import State, to_json

# Runtime parameters: src/graph/graph.py -> parents[2] = repo root.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

# Built-in settings, used only for keys config/config.yaml does not declare or
# declares out of contract. These are the pipeline's floor, not a mirror of the
# file: a value present in the file always wins, which is what makes the
# declaration observable end to end.
_BUILTIN_SETTINGS: Dict[str, Any] = {
    "cjk_threshold": 0.05,
    "max_caller_glossary_terms": 20,
}

# Bounds every declared setting is checked against.
_CJK_THRESHOLD_MIN, _CJK_THRESHOLD_MAX = 0.0, 1.0
_GLOSSARY_CAP_MIN, _GLOSSARY_CAP_MAX = 0, 100


def _runtime_config() -> Dict[str, Any]:
    """Read the runtime parameters from config/config.yaml.

    This is the file the registry loads and passes as Graph(config=...); the
    standalone server (src/api/server.py) reads it here so both deployments run
    on the same declaration. Returns an empty dict — never raises — when the
    file is absent, unreadable, not valid YAML, or not a mapping; the graph
    then runs on its built-in floor. PyYAML is imported lazily because it is a
    framework runtime dependency rather than a module-load coupling of this
    template.
    """
    try:
        import yaml

        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return cast(Dict[str, Any], loaded)


def _config_number(value: Any, lo: float, hi: float) -> Optional[float]:
    """Validate one declared numeric setting: a real number, finite, within [lo, hi].

    Bools, strings, other non-numerics, NaN/Infinity and out-of-range values
    all return None, and the consumer keeps its built-in floor. Rejecting
    non-finite values matters even for a declared setting: NaN compares False
    against every bound, so a NaN threshold would silently disable the
    detection it configures rather than fail.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or not lo <= parsed <= hi:
        return None
    return parsed


def declared_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and flatten the declared runtime settings for the pipeline.

    `config` is what the graph was constructed with — the contents of
    config/config.yaml. Each value is checked for type, finiteness and range;
    an absent or out-of-contract key falls back to the built-in floor rather
    than propagating a value no consumer could use.
    """
    settings: Dict[str, Any] = dict(_BUILTIN_SETTINGS)

    detection = config.get("detection")
    detection = detection if isinstance(detection, dict) else {}
    threshold = _config_number(detection.get("cjk_threshold"), _CJK_THRESHOLD_MIN, _CJK_THRESHOLD_MAX)
    if threshold is not None:
        settings["cjk_threshold"] = threshold

    translation = config.get("translation")
    translation = translation if isinstance(translation, dict) else {}
    cap = _config_number(translation.get("max_caller_glossary_terms"), _GLOSSARY_CAP_MIN, _GLOSSARY_CAP_MAX)
    if cap is not None and cap == int(cap):
        settings["max_caller_glossary_terms"] = int(cap)

    return settings


class TranslationWorkflowGraphNode(GraphNode):
    """GraphNode wrapper for the inner DomainWorkflowGraph.

    Assigned to the `main` backbone slot. Delegates the multi-step domain
    workflow (detect → classify → translate) to the inner graph, and forwards
    the validated runtime settings to it via _parent_config().
    """

    # Re-raise inner graph exceptions as SubgraphError (fail-fast default).
    error_strategy: ClassVar[str] = "propagate"

    # HITL interrupts are handled inside the inner graph.
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        """Bind the validated runtime settings forwarded by the outer graph."""
        super().__init__()
        self._settings: Dict[str, Any] = dict(settings or _BUILTIN_SETTINGS)

    def _parent_config(self) -> Dict[str, Any]:
        """Forward the validated runtime settings to the inner graph.

        The values come from the outer graph's own config — config/config.yaml
        as loaded by the registry (or by the standalone server through
        _runtime_config()) — validated once in declared_settings(). The inner
        graph republishes them into inner state
        (DomainWorkflowGraph._extra_initial_state()) so the domain nodes read a
        live detection threshold rather than a dead declaration.
        """
        return {"configurable": {"settings": dict(self._settings)}}

    def get_subgraph(self) -> Any:
        """Instantiate the inner DomainWorkflowGraph.

        The inner graph receives the validated settings through the BaseGraph
        constructor; its domain NODES still take no constructor arguments and
        read config from state.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request declined by pre_process has no validated input to act on, so
        running the inner graph would only produce a second, vaguer reason for
        the same rejection - and overwrite the specific one already settled.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        """Hand the inner graph the validated document, and bridge the request.

        GraphNode.extract_input() returns a STRING the framework writes into
        the inner state under `user_input`. That field is one the platform's
        input gate rewrites — its personal-name heuristic masks any two
        consecutive capitalised words, the shape of ordinary contract language
        — so the inner pipeline reads the document from the bridged
        document_text field instead and `user_input` serves only as the
        framework-visible copy.

        This is also the last hook that sees the outer state before the inner
        invoke, and the framework does not forward other outer state fields
        into a subgraph (see src/graph/context_bridge.py), so the validated
        document and caller request are stashed here for the inner side to
        pick up.
        """
        document = cast(str, state.get("document_text") or state.get("validated_input") or "")
        set_caller_input_context(
            {
                "document_text": document,
                "caller_request": state.get("caller_request") or "{}",
            }
        )
        return document or cast(str, state.get("user_input") or "")

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map inner graph sub_result fields back into the outer state.

        sub_result is shaped by DomainWorkflowGraph.get_output().
        Returns ONLY the keys this node changes — never the full state.
        """
        return {
            # Outer reason wins: a reason settled before the inner run is the real
            # one, and a plain sub_result.get() would erase it.
            "error_code": state.get("error_code") or sub_result.get("error_code", ""),
            "translated_output": sub_result.get("translated_output"),
            "document_type": sub_result.get("document_type"),
            "source_language": sub_result.get("source_language"),
            "target_language": sub_result.get("target_language"),
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
        }


class ProfServicesTranslationAgent(AgentBaseGraph):
    """Cat 2 outer graph — Professional Services Document Translation Agent.

    Base class: AgentBaseGraph (direct framework inheritance).
    Domain complexity is encapsulated in the inner DomainWorkflowGraph,
    accessed via TranslationWorkflowGraphNode in the `main` slot.

    Runtime configuration: the registry loads config/config.yaml and passes it
    as Graph(config=...); the standalone server does the same via
    _runtime_config(). AgentBaseGraph consumes max_retry from that config for
    retry routing, the validated detection settings reach the inner graph
    through TranslationWorkflowGraphNode, and the caller-contract bounds reach
    the pre_process node through the initial state — so every declared value is
    live in both deployments.
    """

    @property
    def name(self) -> str:
        return "ProfServicesTranslationAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        # The declared settings are validated once here and handed to the main
        # slot, so config/config.yaml is the single source for both the
        # caller-contract bounds and the inner pipeline.
        self._nodes["pre_process"] = ValidateInputNode()
        self._nodes["main"] = TranslationWorkflowGraphNode(settings=declared_settings(self.config))
        self._nodes["post_process"] = SecurityGateOutputNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the validated runtime settings into the outer initial state.

        The node contract takes no config argument and every node is
        constructed without one, so the bound ValidateInputNode needs — the
        caller-glossary entry cap — travels through State.
        """
        return {"runtime_settings": to_json(declared_settings(self.config))}


# Module-level alias for backward compatibility with src/api/server.py
# (which imports `Graph`) and the AgentRegistry module path.
Graph = ProfServicesTranslationAgent
