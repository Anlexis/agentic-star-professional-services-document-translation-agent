"""AgentCore Platform v1.0"""

# Inner domain node — determine the translation direction.
#
# TrustLevel.ANONYMOUS: inner domain nodes run inside the sub-graph, which
# inherits the outer InvocationContext unchanged (no trust escalation). The
# external gate stays on the backbone ValidateInputNode (VERIFIED_EXTERNAL);
# ANONYMOUS here admits the already-validated caller (0 <= any caller trust).
#
# Direction rules:
#   - When the caller pinned target_language, the pin fixes the direction: the
#     source is the other language of the EN↔JA pair. Detection is skipped —
#     the caller's declaration wins over a heuristic.
#   - Otherwise the document is detected: if the fraction of CJK code-point
#     characters exceeds the configured threshold, the document is classified
#     as Japanese; otherwise English. The target is the opposite.
#
# The threshold is declared in config/config.yaml (detection.cjk_threshold) and
# arrives through the runtime_settings state field, seeded by
# DomainWorkflowGraph._extra_initial_state(). An absent or out-of-contract
# value falls back to the built-in floor.

import math
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json

# Unicode CJK Unified Ideographs + Hiragana + Katakana ranges.
_CJK_RANGES = (
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs (basic)
    (0x3400, 0x4DBF),  # CJK Extension A
    (0xFF65, 0xFF9F),  # Halfwidth Katakana
)

# Built-in detection floor: if CJK characters make up >=5 % of the printable
# text, classify as JA. config/config.yaml's detection.cjk_threshold overrides
# this when it declares a finite number in [0, 1].
_DEFAULT_CJK_THRESHOLD = 0.05


def _is_cjk(codepoint: int) -> bool:
    return any(lo <= codepoint <= hi for lo, hi in _CJK_RANGES)


def _detect_language(text: str, threshold: float) -> str:
    """Return 'JA' or 'EN' based on CJK character fraction."""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return "EN"
    cjk_count = sum(1 for c in chars if _is_cjk(ord(c)))
    return "JA" if (cjk_count / len(chars)) >= threshold else "EN"


def _configured_threshold(state: dict[str, Any]) -> float:
    """Read the detection threshold from runtime_settings, fail safe to the floor."""
    settings = from_json(state.get("runtime_settings"), {}) or {}
    value = settings.get("cjk_threshold")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _DEFAULT_CJK_THRESHOLD
    parsed = float(value)
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        return _DEFAULT_CJK_THRESHOLD
    return parsed


class DetectSourceLanguageNode(FunctionNode):
    """Determine the translation direction for the document.

    Sets source_language ("EN"|"JA") and target_language (the opposite),
    honouring a caller-pinned target_language when the validated request
    carries one.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        # The document travels on document_text (the fidelity channel seeded by
        # the graph boundary); validated_input / user_input are the fallbacks
        # for direct node-level use.
        text: str = state.get("document_text") or state.get("validated_input") or state.get("user_input", "") or ""

        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["DetectSourceLanguageNode: document text is empty"],
            }

        request = from_json(state.get("caller_request"), {}) or {}
        pinned_target = request.get("target_language")
        if pinned_target in ("EN", "JA"):
            target = pinned_target
            source = "EN" if target == "JA" else "JA"
        else:
            source = _detect_language(text, _configured_threshold(state))
            target = "EN" if source == "JA" else "JA"

        # Audit: record the translation direction (labels only, never content).
        emit_trace_event(
            "detect_source_language_complete",
            {"source_language": source, "target_language": target},
            state,
        )

        return {
            "source_language": source,
            "target_language": target,
            "status": AgentStatus.SUCCESS.value,
        }
