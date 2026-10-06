"""AgentCore Platform v1.0"""

# Inner domain node — classify the professional services document type and
# assemble the terminology glossary for the translation step.
#
# TrustLevel.ANONYMOUS: inner domain node inside the sub-graph, which inherits
# the outer InvocationContext unchanged. The external gate stays on the backbone
# ValidateInputNode (VERIFIED_EXTERNAL); ANONYMOUS admits the validated caller.
#
# Document taxonomy: contract, proposal, regulatory_filing, client_report.
# A caller-pinned document_type (validated upstream) wins over the keyword
# heuristic. The built-in glossary for the resolved type is then merged with
# the caller's validated glossary_terms — the caller's preferred translation
# wins on a term both declare, because the engagement's own terminology is the
# authority on itself.
#
# The glossary is serialized to a JSON string (dict → Optional[str] for
# msgpack-safe state).

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json

# Keyword sets for each document type (lower-case; matched case-insensitively).
_TYPE_KEYWORDS: dict[str, list[str]] = {
    "contract": [
        "agreement",
        "contract",
        "terms and conditions",
        "clause",
        "indemnification",
        "liability",
        "party",
        "termination",
        "confidential",
        "nda",
        "warranty",
        "契約",
        "合意",
        "条項",
        "免責",
        "解約",
        "秘密保持",
    ],
    "proposal": [
        "proposal",
        "scope of work",
        "deliverable",
        "milestone",
        "engagement",
        "statement of work",
        "sow",
        "budget",
        "timeline",
        "approach",
        "提案",
        "業務範囲",
        "成果物",
        "マイルストーン",
        "予算",
    ],
    "regulatory_filing": [
        "regulatory",
        "filing",
        "compliance",
        "disclosure",
        "financial statement",
        "ifrs",
        "j-gaap",
        "audit",
        "statutory",
        "annual report",
        "sec",
        "規制",
        "開示",
        "財務諸表",
        "監査",
        "法定",
        "有価証券報告書",
    ],
    "client_report": [
        "findings",
        "recommendation",
        "analysis",
        "executive summary",
        "key performance",
        "kpi",
        "dashboard",
        "report",
        "assessment",
        "調査結果",
        "提言",
        "分析",
        "エグゼクティブサマリー",
        "レポート",
    ],
}

# Built-in domain terminology glossaries (EN↔JA, direction-agnostic pairs).
# Stored as json.dumps() string in state["terminology_glossary_json"].
_GLOSSARIES: dict[str, dict[str, str]] = {
    "contract": {
        "indemnification": "損害賠償",
        "termination": "解約",
        "warranty": "保証",
        "confidentiality": "秘密保持",
        "governing law": "準拠法",
        "liability": "責任",
        "party": "当事者",
        "force majeure": "不可抗力",
    },
    "proposal": {
        "scope of work": "業務範囲",
        "deliverable": "成果物",
        "milestone": "マイルストーン",
        "engagement": "エンゲージメント",
        "statement of work": "業務委託書",
        "workstream": "ワークストリーム",
    },
    "regulatory_filing": {
        "IFRS": "国際財務報告基準",
        "J-GAAP": "日本会計基準",
        "disclosure": "開示",
        "financial statement": "財務諸表",
        "audit opinion": "監査意見",
        "going concern": "継続企業",
        "materiality": "重要性",
    },
    "client_report": {
        "key finding": "主要な発見事項",
        "recommendation": "推奨事項",
        "executive summary": "エグゼクティブサマリー",
        "KPI": "重要業績評価指標",
        "benchmark": "ベンチマーク",
        "gap analysis": "ギャップ分析",
    },
}


def _classify(text: str) -> str:
    """Score each document type by keyword hits; return the winning type."""
    lower = text.lower()
    scores: dict[str, int] = {dtype: 0 for dtype in _TYPE_KEYWORDS}
    for dtype, keywords in _TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in lower:
                scores[dtype] += 1
    best = max(scores, key=lambda k: scores[k])
    # Fall back to client_report when no keyword matched
    return best if scores[best] > 0 else "client_report"


class ClassifyDocumentTypeNode(FunctionNode):
    """Classify the document type and assemble the terminology glossary.

    document_type is one of:
      "contract", "proposal", "regulatory_filing", "client_report"
    — the caller's validated pin when present, the keyword heuristic otherwise.

    terminology_glossary_json is the JSON-encoded merge of the built-in
    glossary for that type with the caller's validated glossary_terms
    (caller entries win on a shared term).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        text: str = state.get("document_text") or state.get("validated_input") or state.get("user_input", "") or ""

        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyDocumentTypeNode: document text is empty"],
            }

        request = from_json(state.get("caller_request"), {}) or {}
        pinned_type = request.get("document_type")
        if pinned_type in _GLOSSARIES:
            doc_type = pinned_type
        else:
            doc_type = _classify(text)

        glossary = dict(_GLOSSARIES.get(doc_type, {}))
        caller_terms = request.get("glossary_terms")
        caller_term_count = 0
        if isinstance(caller_terms, dict):
            # Upstream validation bounded every entry; the caller's preferred
            # translation wins on a term both glossaries declare.
            caller_term_count = len(caller_terms)
            glossary.update(caller_terms)

        # Serialize dict → JSON string for msgpack-safe state storage
        glossary_json = json.dumps(glossary, ensure_ascii=False)

        # Audit: record the classification outcome (labels and counts only).
        emit_trace_event(
            "classify_document_type_complete",
            {
                "document_type": doc_type,
                "pinned": pinned_type in _GLOSSARIES,
                "glossary_terms": len(glossary),
                "caller_terms": caller_term_count,
            },
            state,
        )

        return {
            "document_type": doc_type,
            "terminology_glossary_json": glossary_json,
            "status": AgentStatus.SUCCESS.value,
        }
