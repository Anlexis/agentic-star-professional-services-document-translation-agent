"""AgentCore Platform v1.0"""

# Inner domain node — translate the professional services document with
# domain terminology consistency enforcement.
#
# TrustLevel.ANONYMOUS: inner domain node inside the sub-graph, which inherits
# the outer InvocationContext unchanged. The external gate stays on the backbone
# ValidateInputNode (VERIFIED_EXTERNAL); ANONYMOUS admits the validated caller.
#
# Reads terminology_glossary_json as a JSON string and deserializes it to a
# dict for glossary lookup.
#
# Fidelity invariant (enforced by construction, verified in the test suite):
# outside glossary-term substitution the document body is emitted byte for
# byte — numbers, decimals, reference codes and dates are never reformatted,
# rounded or rewritten. A translation that alters a figure falsifies the
# document, so no numeric post-processing exists on this path.
#
# Audit: emits a trace event recording the translation operation provenance
# (document_type, direction, glossary term count) — NOT the translated content.
# Translation content is confidential under NDA; only audit metadata is logged.

import json
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class TranslateWithDomainTermsNode(FunctionNode):
    """Translate the document with domain-specific terminology enforcement.

    Reads:
      document_text              — document text to translate (fidelity channel;
                                   validated_input / user_input as fallbacks)
      source_language            — "EN" | "JA"
      target_language            — "EN" | "JA"
      document_type              — used for audit metadata
      terminology_glossary_json  — JSON dict (domain term → translation)

    Writes:
      translated_output          — translated document with glossary terms applied
      status
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        text: str = state.get("document_text") or state.get("validated_input") or state.get("user_input", "") or ""
        source_lang: str = state.get("source_language", "EN") or "EN"
        target_lang: str = state.get("target_language", "JA") or "JA"
        doc_type: str = state.get("document_type", "client_report") or "client_report"

        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["TranslateWithDomainTermsNode: document text is empty"],
            }

        # Deserialize terminology glossary from JSON string
        raw_glossary_json = state.get("terminology_glossary_json") or "{}"
        try:
            glossary: dict[str, str] = json.loads(raw_glossary_json)
        except (json.JSONDecodeError, ValueError):
            glossary = {}

        # Stub translation: apply glossary term substitutions, then annotate
        # the result with the translation direction for downstream verification.
        # In production this call is replaced by an LLM translation service.
        translated = _apply_glossary(text, glossary, source_lang, target_lang)

        # Audit translation provenance — NOT content (NDA compliance).
        audit_payload = {
            "event": "translation_executed",
            "source_language": source_lang,
            "target_language": target_lang,
            "document_type": doc_type,
            "glossary_terms_applied": len(glossary),
            "character_count": len(translated),
        }
        emit_trace_event("translation_audit", audit_payload, state)

        return {
            "translated_output": translated,
            "status": AgentStatus.SUCCESS.value,
        }


def _apply_glossary(
    text: str,
    glossary: dict[str, str],
    source_lang: str,
    target_lang: str,
) -> str:
    """Apply glossary term substitutions to the translated text.

    Substitutions are applied longest-match-first to prevent partial overlaps.
    In production this wraps an LLM call; the stub returns the annotated source
    text with glossary replacements for unit-testability. Text outside glossary
    matches is preserved byte for byte.
    """
    if not text:
        return text

    # Build annotation prefix indicating the translation direction
    direction_tag = f"[{source_lang}→{target_lang}] "

    # Apply known glossary terms (longest keys first to avoid partial matches)
    result = text
    for term in sorted(glossary.keys(), key=len, reverse=True):
        translation = glossary[term]
        result = re.sub(re.escape(term), translation, result, flags=re.IGNORECASE)

    return direction_tag + result
