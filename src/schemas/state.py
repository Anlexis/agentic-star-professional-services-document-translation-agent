"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never Pydantic BaseModel. LangGraph
# checkpoints use msgpack serialization; Pydantic objects cause silent
# corruption. Extend AgentState with agent-specific fields only. Do NOT add
# credentials, secrets, or Pydantic models.
#
# Serialization rule: dict/list values → Optional[str] (JSON-encoded).
# Producers call json.dumps() before assigning; consumers call json.loads().
# This keeps the state flat and msgpack-safe across checkpoints.

import json
from typing import Any, Optional

from framework.schemas.agent_state import AgentState


def to_json(obj: Any) -> Optional[str]:
    """Serialize a dict or list to a JSON string. Returns None if obj is None."""
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False)


def from_json(s: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON string to a Python dict or list.

    Returns *default* when the value is absent, empty, or not valid JSON — a
    checkpoint that carries a malformed field must not take the node down.
    """
    if not s:
        return default
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return default


class State(AgentState):
    """SVC-C2-008 domain state.

    Domain fields (all Optional[str] — dict-valued fields are JSON-encoded):
      validated_input           — accepted document text (set by ValidateInputNode)
      document_text             — the same text on the fidelity channel: a domain
                                  state field the platform input gate does not
                                  rewrite, so capitalised terminology survives to
                                  the translation pipeline verbatim
      caller_request            — JSON-encoded validated caller request
                                  (document_type pin, target_language pin,
                                  glossary_terms mapping)
      runtime_settings          — JSON-encoded validated runtime settings from
                                  config/config.yaml (cjk_threshold,
                                  max_caller_glossary_terms)
      source_language           — detected source language: "EN" | "JA"
      target_language           — computed target language: "EN" | "JA"
      document_type             — classified type: "contract" | "proposal"
                                  | "regulatory_filing" | "client_report"
      terminology_glossary_json — JSON-encoded dict mapping domain terms to
                                  their target-language equivalents
      translated_output         — final translated document text

    Shared fields (user_input, status, session_id, node_history, error_log,
    hitl_*, etc.) are inherited from AgentState.
    """

    # ValidateInputNode outputs
    validated_input: Optional[str]
    document_text: Optional[str]
    caller_request: Optional[str]

    # Validated runtime settings from config/config.yaml, seeded into the
    # initial state by the outer graph and republished into the inner graph's
    # initial state by DomainWorkflowGraph._extra_initial_state(). Domain nodes
    # read it with from_json(), which keeps every node at
    # execute(self, state) -> dict.
    runtime_settings: Optional[str]

    # DetectSourceLanguageNode outputs
    source_language: Optional[str]  # "EN" | "JA"
    target_language: Optional[str]  # "EN" | "JA"

    # ClassifyDocumentTypeNode outputs
    document_type: Optional[str]  # "contract" | "proposal" | "regulatory_filing" | "client_report"

    # Terminology glossary serialized as JSON string (dict → Optional[str])
    # Producers: json.dumps({"IFRS": "国際財務報告基準", ...})
    # Consumers: json.loads(state.get("terminology_glossary_json") or "{}")
    terminology_glossary_json: Optional[str]

    # TranslateWithDomainTermsNode output
    translated_output: Optional[str]
    error_code: Optional[str]
