"""AgentCore Platform v1.0"""

# Output-gate node — post_process backbone slot.
# Implements the output security gate inline in execute() via the module-level
# _security_gate_output() helper (NOT an instance method). Security checks
# always belong in execute() or module-level helpers, never in framework hook
# methods that the framework auto-wraps.
#
# Checks performed:
#   1. Reject translated output containing raw credential patterns.
#   2. Reject output that is unreasonably large (potential data-exfiltration).
# Audit: emits a gate_pass or gate_fail trace event.
#
# Output fidelity note: this gate REFUSES a violating document — it never
# rewrites one. The translation contract promises the document body byte for
# byte outside glossary substitutions, so there is no redaction or numeric
# normalisation on this path; a blocked output yields an error and a withheld
# notice in place of the document.
#
# CONTAINMENT — why the refusal branch clears state rather than only erroring:
#
#   By the time this gate runs, the main slot has already mapped the translated
#   document into the OUTER state: TranslationWorkflowGraphNode.merge_output()
#   writes it to both `translated_output` and `result`. The base envelope then
#   resolves the released text as
#       formatted_output or result
#   so refusing takes two separate pieces of work, and doing only the first
#   ships the document the gate just refused:
#
#   1. `formatted_output` must be set to a NON-EMPTY value. An empty string is
#      falsy, so `"" or result` evaluates to `result` — writing "" does not
#      withhold the translation, it hands the translation over. The withheld
#      notice below is deliberately truthy for exactly that reason.
#   2. Every field still holding a representation of the translated document
#      must be cleared in the SAME delta. An ERROR status on its own leaves
#      `result` and `translated_output` readable.
#
#   The returned message names the screened FIELD and nothing else. Quoting the
#   offending text would put it back into the very delta this node returns,
#   where the framework's own output scan reads it, raises, and replaces the
#   whole delta — clearing included — with a generic node error. The refusal
#   detail therefore goes to the operator log and the audit trail, which are
#   not caller-visible surfaces.

import logging
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

logger = logging.getLogger(__name__)

# Output ceiling: refuse to emit translated documents larger than 200 KB.
# Prevents accidental bulk re-emission of confidential engagement content.
_MAX_OUTPUT_CHARS = 200_000

# Credential-leak pattern: same family as the ValidateInputNode input screen.
_CREDENTIAL_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9\-._~+/]{20,}"
    r"|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC )?PRIVATE KEY)",
    re.IGNORECASE,
)


# The state field this gate screens — and the only location named in the
# caller-visible error.
_SCREENED_FIELD = "translated_output"

# Truthy by design. See the CONTAINMENT note above: an empty formatted_output
# is falsy and re-activates the base envelope's fallback to `result`, which is
# the un-screened translation. This placeholder is what the caller receives
# instead, and it carries no document content of any kind.
_WITHHELD_NOTICE = "[output withheld: the translated document did not pass the output gate]"

# Every outer-state field that can carry a representation of the translated
# document, cleared together on a violation. Derived from the one route a
# translation takes to a caller: TranslationWorkflowGraphNode.merge_output(),
# which maps the inner graph's rendered translation into `translated_output`
# and `result`. The other fields it maps — document_type, source_language,
# target_language — are inert enumerations carrying provenance, not document
# content, and are deliberately left in place so the refusal stays diagnosable.
# tests/proof_of_boundary/test_pb_output_containment.py pins this list against
# merge_output() so a field added there cannot quietly escape the clearing.
_OUTPUT_BEARING_FIELDS: tuple[str, ...] = (
    "result",
    "translated_output",
)


def _security_gate_output(output: str, state: dict[str, Any]) -> tuple[bool, str]:
    """Module-level output gate (NOT an instance method).

    Returns (safe: bool, reason: str).
    Called from SecurityGateOutputNode.execute() — never as a framework hook.

    Checks:
      - Size ceiling: translated output must not exceed _MAX_OUTPUT_CHARS.
      - Credential leak: translated output must not contain credential patterns.
    """
    if len(output) > _MAX_OUTPUT_CHARS:
        return False, (
            f"output gate: translated output exceeds size ceiling " f"({len(output)} > {_MAX_OUTPUT_CHARS} chars)"
        )
    if _CREDENTIAL_PATTERN.search(output):
        return False, "output gate: translated output contains a credential pattern (data-leak block)"
    return True, "ok"


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class SecurityGateOutputNode(FunctionNode):
    """Output gate: validate translated output before returning to the caller.

    Post-process backbone slot. Runs the module-level _security_gate_output()
    check then emits an audit trace for every invocation.
    """

    # TrustLevel.ANONYMOUS: the trust gate is permissive here.
    # The outer backbone's ValidateInputNode (pre_process, VERIFIED_EXTERNAL) is
    # the external trust gate; nodes downstream are invoked by the framework
    # engine (outer state always carries the originating caller's trust level,
    # so requiring INTERNAL would cause a trust-gate denial).
    # The real output security for this node is the module-level gate function
    # _security_gate_output() called inside execute(), not the trust restriction.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        #
        # The document-bearing fields are cleared here for the same reason the
        # gate's own refusal clears them, and by the same mechanism: the base
        # envelope resolves the released text as `formatted_output or result`,
        # so the reason sentence has to be truthy to win, and no field may be
        # left holding something that reads as a product. On this path nothing
        # was translated at all — the main slot never ran the inner graph — so
        # clearing them is a statement of that, not a redaction.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            declined: dict[str, Any] = {field: None for field in _OUTPUT_BEARING_FIELDS}
            declined["formatted_output"] = message
            declined["status"] = AgentStatus.SUCCESS.value
            declined["error_code"] = marker
            return declined
        translated: str = state.get("translated_output", "") or ""
        doc_type: str = state.get("document_type", "") or ""
        source_lang: str = state.get("source_language", "") or ""
        target_lang: str = state.get("target_language", "") or ""

        # Output gate (module-level, NOT instance method)
        safe, reason = _security_gate_output(translated, state)

        if not safe:
            # The specific reason goes to the operator log and the audit trail
            # — neither is a caller-visible surface. The returned delta names
            # the screened field only (see the CONTAINMENT note above).
            logger.error("SecurityGateOutputNode: %s blocked - %s", _SCREENED_FIELD, reason)
            emit_trace_event(
                "s3_gate_fail",
                {"reason": reason, "document_type": doc_type},
                state,
            )
            blocked: dict[str, Any] = {field: None for field in _OUTPUT_BEARING_FIELDS}
            blocked["formatted_output"] = _WITHHELD_NOTICE
            blocked["status"] = AgentStatus.ERROR.value
            blocked["error_log"] = [f"SecurityGateOutputNode: {_SCREENED_FIELD} failed the output gate"]
            return blocked

        # Audit: gate pass (log provenance metadata, NOT translated content)
        emit_trace_event(
            "s3_gate_pass",
            {
                "document_type": doc_type,
                "source_language": source_lang,
                "target_language": target_lang,
                "output_chars": len(translated),
            },
            state,
        )

        return {
            "formatted_output": translated,
            "status": AgentStatus.SUCCESS.value,
        }
