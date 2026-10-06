"""AgentCore Platform v1.0"""

# Input-validation node — pre_process backbone slot; the external-facing gate.
#
# Responsibilities:
#   - Enforce VERIFIED_EXTERNAL trust (required_trust_level — the trust gate)
#   - Accept the document from either channel, validate it, and fail CLOSED on
#     any violation, naming the FIELD and never echoing the VALUE
#   - Refuse credential material, instruction-override payloads and
#     chat-template control tokens before any domain processing
#   - Validate every structured caller field against explicit bounds
#   - Publish the validated document and caller request for the pipeline
#
# Caller data arrives on two channels and both are validated here:
#
#   input          the document text (default channel). The platform's input
#                  gate masks contact identifiers AND any run of two or more
#                  consecutive capitalised words on this field before this node
#                  runs — which is the shape of ordinary professional-services
#                  language ("Master Service Agreement", "Change Management") —
#                  so capitalised terminology submitted here reaches the
#                  pipeline as mask tokens.
#   input_context  the structured channel, delivered verbatim. Fields:
#                    document        the document text on the fidelity channel;
#                                    capitalised terminology survives intact.
#                                    Screened HERE instead: contact identifiers
#                                    (email, phone, national/payment IDs) are
#                                    REFUSED rather than masked — a caller whose
#                                    document carries them uses the default
#                                    channel, where they are masked.
#                    document_type   inert slug pinning the classification:
#                                    contract | proposal | regulatory_filing |
#                                    client_report
#                    target_language inert slug pinning the direction: en | ja
#                    glossary_terms  mapping of the engagement's own terminology
#                                    (term -> preferred translation), entry-
#                                    capped; merged over the built-in glossary
#
# The fidelity channel exists because the platform mask's personal-name
# heuristic matches any two consecutive capitalised words, which is exactly the
# shape of a service line or agreement name — masking would destroy the very
# terminology this template exists to preserve. The `name` detector type is
# therefore deliberately NOT screened on this channel; every screened type is a
# contact or payment identifier whose shape no professional-services term has.
#
# When no structured request is submitted the run degrades to the default
# channel and the built-in glossary. There is no path on which unvalidated
# caller data reaches the pipeline.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, TOO_LONG
from src.services.progress import emit_progress

# Maximum document size: 100 000 characters (~100 KB of text).
_MAX_DOCUMENT_CHARS = 100_000

# Caller-glossary bounds. The entry cap is declared in config/config.yaml
# (translation.max_caller_glossary_terms) and travels here through the
# runtime_settings state field; this is the built-in floor.
_DEFAULT_MAX_GLOSSARY_TERMS = 20
_MAX_GLOSSARY_ENTRY_CHARS = 80

# Enumerations for the inert slug fields.
_DOCUMENT_TYPES = frozenset({"contract", "proposal", "regulatory_filing", "client_report"})
_TARGET_LANGUAGES = {"en": "EN", "ja": "JA"}

# Credential material must be refused before domain processing. Matches API
# keys, bearer tokens, cloud access keys, and private-key blocks.
_CREDENTIAL_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9\-._~+/]{20,}"
    r"|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC )?PRIVATE KEY)",
    re.IGNORECASE,
)

# Chat-template control tokens: text that forges a model turn boundary rather
# than saying anything. Screened as a CLASS — any <|...|> token, the
# bracket-form and angle-form role markers, and role-tag pairs — on every
# caller-supplied string, keys included, after parsing (an escaped payload
# arrives decoded).
_CONTROL_TOKEN_RE = re.compile(
    r"<\|[^|>]{1,32}\|>"  # <|im_start|>, <|im_end|>, <|endoftext|>, ...
    r"|\[/?(?:INST|SYS)\]"  # [INST], [/INST], [SYS], [/SYS]
    r"|<<\s*/?\s*SYS\s*>>"  # <<SYS>>, <</SYS>>
    r"|<\s*/?\s*(?:system|user|assistant)\s*>"  # <system>, </user>, ...
    r"|###\s*(?:system|instruction)\b",
    re.IGNORECASE,
)

# Instruction-override phrasings: text addressed to a MODEL rather than a
# document submitted for translation. The platform input gate refuses
# high-confidence payloads on the default channel too, but this template must
# not depend on that — where that gate is absent or configured off, an
# unchecked payload would reach the translation path. Refusal is stated in
# terms of BEHAVIOUR (error status, nothing published), never a gate's wording.
#
# Deliberately narrow: each alternative requires the imperative override shape,
# so genuine contract language that happens to use these words is unaffected
# ("the Contractor shall act as an agent of the Client", "prior instructions
# from the Steering Committee govern change requests" both pass — see the
# probes in tests/unit/test_caller_contract.py).
_INJECTION_RE = re.compile(
    r"(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instruction|instructions|prompt|prompts|rule|rules|direction|directions|context)"
    r"|(?:reveal|show|print|repeat|output|disclose)\s+(?:me\s+)?(?:your|the)\s+"
    r"(?:system\s+prompt|system\s+message|instructions|initial\s+prompt)"
    r"|you\s+are\s+now\s+(?:a|an)\s"
    r"|act\s+as\s+(?:if\s+you\s+are\s+)?(?:a\s+|an\s+)?(?:developer|admin|root)\s+mode"
    r"|override\s+(?:your|the)\s+(?:instruction|instructions|rules|safety)",
    re.IGNORECASE,
)

# Glossary entries are the only caller free text that steers the rendered
# translation, so they are locked to a term alphabet: word characters in any
# script — a Japanese term is accepted as readily as an English one — plus
# space and the punctuation professional-services terminology uses
# ("Statement of Work", "Attorney's Fees", "Post-Merger Integration",
# "Finance (Controllership)"). Everything else — quotes, brackets, hashes,
# asterisks, backticks, angle brackets, pipes, backslashes, colons, control
# characters and every newline — is refused, so a term can neither carry markup
# nor break the line structure of the document it is substituted into.
_TERM_RE = re.compile(r"^[\w &.,()/+'-]+$")

# Contact identifiers have no place in a terminology pair, and the term
# alphabet alone does not exclude them (it permits digits, spaces, dots and
# hyphens). Only high-precision detector types are screened; the personal-name
# heuristic is excluded because it matches any two Title Case words — the shape
# of the very terminology this channel exists to carry.
_SCREENED_PII_TYPES = frozenset({"email", "phone_jp", "phone_us", "ssn_us", "credit_card", "my_number_jp"})

# Client / engagement reference shapes are refused in glossary entries: a
# terminology pair carries language, never an engagement identifier.
_IDENT_GUARD_L = r"(?<![A-Za-z0-9_-])"
_IDENT_GUARD_R = r"(?![A-Za-z0-9_-])"
_CLIENT_IDENTIFIER_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    (
        "engagement_reference",
        re.compile(_IDENT_GUARD_L + r"[A-Za-z0-9-]*[A-Z]{2,6}-\d{4,8}[A-Za-z0-9-]*" + _IDENT_GUARD_R),
    ),
    (
        "client_reference_number",
        re.compile(_IDENT_GUARD_L + r"\d{4}[- ]?\d{4}[- ]?\d{2,11}" + _IDENT_GUARD_R),
    ),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
]

# Control characters stripped from document text before it is measured.
# Tab, newline and carriage return are document structure and are kept.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# ---------------------------------------------------------------------------
# How a rejection is reported
# ---------------------------------------------------------------------------
#
# Every validator below returns a reason CLASS alongside its message, because
# the two kinds of rejection this node produces must not be reported the same
# way.
#
#   A value the caller can correct — an absent document, one over the size
#   bound, a field of the wrong type, a slug outside its enumeration, a
#   glossary entry outside the term alphabet — carries one of the codes below.
#   The run COMPLETES holding that code, so the reason reaches the caller and a
#   corrected request can be sent on the same conversation.
#
#   Content the template REFUSES — instruction-override or chat-template
#   control text, credential material, a contact identifier on the fidelity
#   channel — carries _TERMINAL and the run ends in error, exactly as before.
#   A refusal is not a value to correct, and reporting it as one would read as
#   an invitation to reword the payload until it is accepted.
#
# The class travels as a return value rather than being recovered from the
# message text: the messages are written for the audit log and change freely,
# and a refusal silently re-classified by an edited sentence is the one failure
# this split exists to make impossible.
_TERMINAL = ""
_CODE_EMPTY_INPUT = "EMPTY_INPUT"
_CODE_TOO_LONG = "QUESTION_TOO_LONG"
_CODE_INVALID_REQUEST = "INVALID_REQUEST"

# Reason code -> the phase label a caller watching the run sees. Never the
# rejected value, and never the audit message.
_PROGRESS_MESSAGES = {
    _CODE_EMPTY_INPUT: EMPTY_INPUT,
    _CODE_TOO_LONG: TOO_LONG,
    _CODE_INVALID_REQUEST: INPUT_REJECTED,
}


def _hostile_content(text: str) -> Optional[str]:
    """Return a refusal reason when the text carries control or override content."""
    if _CONTROL_TOKEN_RE.search(text):
        return "control-token content"
    if _INJECTION_RE.search(text):
        return "instruction-override content"
    return None


def _screened_pii(text: str) -> bool:
    """True when the text carries a screened contact/payment identifier."""
    return any(f.get("type") in _SCREENED_PII_TYPES for f in detect_pii(text))


def _validate_document(raw: Any, field: str, fidelity: bool) -> Tuple[Optional[str], Optional[str], str]:
    """Validate one document text. Returns (text, error, code). Fail CLOSED.

    `code` is the reason class described above: a correctable-value code, or
    _TERMINAL for the three refusals — credential material, override/control
    content, and a contact identifier on the fidelity channel — that a reworded
    request must not be invited to retry.

    The fidelity channel (input_context.document) additionally refuses contact
    identifiers: on the default channel the platform masks them before this
    node runs, but nothing rewrites the fidelity channel, so a document that
    carries them is refused with the channel named — never echoed, never
    silently altered.
    """
    if not isinstance(raw, str):
        return None, f"ValidateInputNode: {field} must be a string document", _CODE_INVALID_REQUEST
    text = _CONTROL_CHARS_RE.sub("", raw).strip()
    if not text:
        return None, f"ValidateInputNode: {field} is empty or whitespace-only", _CODE_EMPTY_INPUT
    if len(text) > _MAX_DOCUMENT_CHARS:
        return (
            None,
            (f"ValidateInputNode: {field} exceeds maximum size " f"({len(text)} > {_MAX_DOCUMENT_CHARS} chars)"),
            _CODE_TOO_LONG,
        )
    if _CREDENTIAL_PATTERN.search(text):
        return None, f"ValidateInputNode: {field} contains credential material", _TERMINAL
    reason = _hostile_content(text)
    if reason:
        return None, f"ValidateInputNode: {field} refused - {reason}", _TERMINAL
    if fidelity and _screened_pii(text):
        return (
            None,
            (
                f"ValidateInputNode: {field} must not contain contact identifiers; "
                "submit such documents on the default input channel, where they are masked"
            ),
            _TERMINAL,
        )
    return text, None, _TERMINAL


def _validate_glossary_entry(value: Any, field: str) -> Tuple[Optional[str], Optional[str], str]:
    """Validate one glossary term or translation. Returns (value, error, code).

    Fail CLOSED: only a string of the term alphabet within the length limit is
    accepted. The rejected value is never echoed — the error names the entry
    position and restates the contract.

    The term alphabet, the length limit and the engagement-identifier shapes
    are bounds ON THE FIELD: a caller reads the contract, edits the entry and
    sends the request again, so they carry a correctable-value code. Override
    or control content and a detected contact identifier are refusals of the
    CONTENT and stay terminal.
    """
    contract = (
        f"ValidateInputNode: {field} must be 1-{_MAX_GLOSSARY_ENTRY_CHARS} characters "
        "of letters, digits, space and . , ( ) / & + ' -"
    )
    if not isinstance(value, str) or not value.strip():
        return None, contract, _CODE_INVALID_REQUEST
    term = value.strip()
    if len(term) > _MAX_GLOSSARY_ENTRY_CHARS or not _TERM_RE.fullmatch(term):
        return None, contract, _CODE_INVALID_REQUEST
    reason = _hostile_content(term)
    if reason:
        return None, f"ValidateInputNode: {field} refused - {reason}", _TERMINAL
    if _screened_pii(term):
        return None, f"ValidateInputNode: {field} must not contain contact identifiers", _TERMINAL
    for name, pattern in _CLIENT_IDENTIFIER_PATTERNS:
        if pattern.search(term):
            return (
                None,
                (f"ValidateInputNode: {field} must not contain a client or engagement identifier ({name})"),
                _CODE_INVALID_REQUEST,
            )
    return term, None, _TERMINAL


def _validate_request(
    input_context: Any, max_glossary_terms: int
) -> Tuple[Dict[str, Any], Optional[str], Optional[str], str]:
    """Validate the structured caller request.

    Returns (record, document, error, code). An absent or empty channel degrades to
    the default channel and the built-in glossary. Any field that IS supplied
    must satisfy its bound; there is no partial acceptance and no clamping.
    Every string in the mapping is screened — field NAMES included, because a
    key is caller text too; a hostile key is refused by position, its content
    never echoed.
    """
    record: Dict[str, Any] = {}
    if input_context in (None, {}):
        return record, None, None, _TERMINAL
    if not isinstance(input_context, dict):
        return {}, None, "ValidateInputNode: input_context must be an object", _CODE_INVALID_REQUEST

    for position, key in enumerate(input_context):
        # Two unrelated rejections share this message. A key of the wrong TYPE
        # is a shape the caller can fix; a key carrying override or control
        # content is a refusal and must not be reported as correctable.
        if not isinstance(key, str) or _hostile_content(key):
            return (
                {},
                None,
                (
                    f"ValidateInputNode: input_context key #{position} refused - "
                    "field names must be plain identifiers"
                ),
                _CODE_INVALID_REQUEST if not isinstance(key, str) else _TERMINAL,
            )

    document: Optional[str] = None
    if "document" in input_context:
        document, error, code = _validate_document(input_context.get("document"), "document", fidelity=True)
        if error:
            return {}, None, error, code

    if "document_type" in input_context:
        doc_type = input_context.get("document_type")
        if not isinstance(doc_type, str) or doc_type not in _DOCUMENT_TYPES:
            return (
                {},
                None,
                (
                    "ValidateInputNode: document_type must be one of contract, proposal, "
                    "regulatory_filing, client_report"
                ),
                _CODE_INVALID_REQUEST,
            )
        record["document_type"] = doc_type

    if "target_language" in input_context:
        target = input_context.get("target_language")
        if not isinstance(target, str) or target.lower() not in _TARGET_LANGUAGES:
            return {}, None, "ValidateInputNode: target_language must be en or ja", _CODE_INVALID_REQUEST
        record["target_language"] = _TARGET_LANGUAGES[target.lower()]

    if "glossary_terms" in input_context:
        raw_glossary = input_context.get("glossary_terms")
        if not isinstance(raw_glossary, dict):
            return (
                {},
                None,
                "ValidateInputNode: glossary_terms must be an object of term pairs",
                _CODE_INVALID_REQUEST,
            )
        if len(raw_glossary) > max_glossary_terms:
            return (
                {},
                None,
                (f"ValidateInputNode: glossary_terms must hold {max_glossary_terms} entries or fewer"),
                _CODE_INVALID_REQUEST,
            )
        glossary: Dict[str, str] = {}
        for index, (raw_term, raw_translation) in enumerate(raw_glossary.items()):
            term, error, code = _validate_glossary_entry(raw_term, f"glossary_terms[{index}] term")
            if error:
                return {}, None, error, code
            translation, error, code = _validate_glossary_entry(raw_translation, f"glossary_terms[{index}] translation")
            if error:
                return {}, None, error, code
            assert term is not None and translation is not None
            glossary[term] = translation
        record["glossary_terms"] = glossary

    return record, document, None, _TERMINAL


class ValidateInputNode(FunctionNode):
    """Trust gate, caller-data contract and document screen.

    Pre-process backbone slot. TrustLevel.VERIFIED_EXTERNAL enforces that only
    authenticated callers can submit documents for translation. Rejects empty /
    oversized / credential-carrying / override-carrying documents, refuses a
    caller field that breaks its bound, and publishes the validated document
    and request.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _reject(self, state: dict[str, Any], error: str, code: str) -> dict[str, Any]:
        """Stop the request here. Nothing is published on either path.

        `code` decides HOW the stop is reported, and the validators — not this
        method — decide the code. A correctable value COMPLETES the run holding
        the code, so the reason reaches the caller and the request can be sent
        again on the same conversation. A refusal carries _TERMINAL and ends the
        run in error, so it is never presented as something rewording would get
        past.

        The audit message goes to error_log on both paths: it names the field
        and the contract, and it is not a caller-facing surface.
        """
        emit_trace_event("validate_input_rejected", {"code": code or "refused"}, state)
        if code:
            emit_progress(_PROGRESS_MESSAGES.get(code, INPUT_REJECTED))
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": code,
                "error_log": [error],
            }
        return {"status": AgentStatus.ERROR.value, "error_log": [error]}

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        user_input: str = state.get("user_input", "") or ""
        input_context = state.get("input_context", {})  # read-only

        settings = from_json(state.get("runtime_settings"), {}) or {}
        max_glossary_terms = settings.get("max_caller_glossary_terms")
        if not isinstance(max_glossary_terms, int) or max_glossary_terms < 0:
            max_glossary_terms = _DEFAULT_MAX_GLOSSARY_TERMS

        record, fidelity_document, error, code = _validate_request(input_context, max_glossary_terms)
        if error:
            return self._reject(state, error, code)

        document: Optional[str] = fidelity_document
        if document is None:
            document, error, code = _validate_document(user_input, "document", fidelity=False)
            if error:
                return self._reject(state, error, code)
        assert document is not None

        # Audit: record that a document passed validation (size and request
        # shape only — never document content).
        emit_trace_event(
            "validate_input_complete",
            {
                "input_chars": len(document),
                "fidelity_channel": fidelity_document is not None,
                "glossary_terms": len(record.get("glossary_terms") or {}),
                "has_document_type": "document_type" in record,
                "has_target_language": "target_language" in record,
            },
            state,
        )

        return {
            "validated_input": document,
            "document_text": document,
            "caller_request": to_json(record),
            "status": AgentStatus.SUCCESS.value,
        }
