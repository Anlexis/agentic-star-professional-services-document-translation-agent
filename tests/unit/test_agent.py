"""SVC-C2-008 — Unit tests for all domain nodes.

Test strategy:
  - Each node's execute() is called directly (bypassing the framework's __call__
    trust gate and hook chain) — strictly a domain-logic unit test.
  - emit_trace_event is patched AT THE NODE MODULE LEVEL (never via sys.modules
    stubs) to prevent audit side-effects.  Tests that verify audit behaviour
    re-patch with a recording callable.
  - Dict-valued state fields are Optional[str] (JSON-encoded); tests validate
    both the producer side (json.dumps) and the consumer side (json.loads).
"""

from __future__ import annotations

import json

import pytest

from framework.schemas.agent_status import AgentStatus

# ---------------------------------------------------------------------------
# Autouse fixture: silence audit calls at node-module scope
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_emit(monkeypatch):
    """Patch emit_trace_event at the node module level — never via sys.modules."""
    for mod in (
        "src.nodes.validate_input_node",
        "src.nodes.detect_source_language_node",
        "src.nodes.classify_document_type_node",
        "src.nodes.translate_with_domain_terms_node",
        "src.nodes.security_gate_output_node",
    ):
        monkeypatch.setattr(mod + ".emit_trace_event", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# ValidateInputNode — input-validation gate (document channels)
# ---------------------------------------------------------------------------


class TestValidateInputNode:
    """TC-V01 – TC-V09: ValidateInputNode document validation."""

    @pytest.fixture
    def node(self):
        from src.nodes.validate_input_node import ValidateInputNode

        return ValidateInputNode()

    def test_empty_input_returns_error(self, node):
        """TC-V01: empty document → declined, carrying the reason."""
        result = node.execute({"user_input": ""})
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can correct the value and send the request again.
        assert result.get("error_code")
        assert "empty" in result["error_log"][0].lower()

    def test_whitespace_only_returns_error(self, node):
        """TC-V02: whitespace-only document → declined, carrying the reason."""
        result = node.execute({"user_input": "   \t\n  "})
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can correct the value and send the request again.
        assert result.get("error_code")

    def test_oversized_document_returns_error(self, node):
        """TC-V03: document > 100 000 chars → declined, carrying the reason."""
        big_doc = "a" * 100_001
        result = node.execute({"user_input": big_doc})
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can correct the value and send the request again.
        assert result.get("error_code")
        assert any("size" in e.lower() or "exceeds" in e.lower() for e in result["error_log"])

    def test_credential_api_key_blocked(self, node):
        """TC-V04: API key pattern → ERROR (credential block)."""
        payload = "Please translate: sk-" + "A" * 30
        result = node.execute({"user_input": payload})
        assert result["status"] == AgentStatus.ERROR.value
        assert any("credential" in e.lower() for e in result["error_log"])

    def test_credential_bearer_token_blocked(self, node):
        """TC-V05: bearer token pattern → ERROR."""
        payload = "Bearer " + "x" * 30 + " is the token, please translate this."
        result = node.execute({"user_input": payload})
        assert result["status"] == AgentStatus.ERROR.value

    def test_valid_input_accepted(self, node):
        """TC-V06: normal document → SUCCESS + validated_input preserved."""
        doc = "This contract agreement sets out the terms and conditions between the parties."
        result = node.execute({"user_input": doc})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] == doc
        assert result["document_text"] == doc

    def test_missing_user_input_key_returns_error(self, node):
        """TC-V07: state has no user_input key → treated as empty → declined."""
        result = node.execute({})
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can correct the value and send the request again.
        assert result.get("error_code")

    def test_fidelity_document_wins_over_user_input(self, node):
        """TC-V08: input_context.document replaces user_input as the source."""
        doc = "The Master Service Agreement includes a Force Majeure clause."
        result = node.execute({"user_input": "see attached", "input_context": {"document": doc}})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["document_text"] == doc

    def test_fidelity_document_screens_credentials_too(self, node):
        """TC-V09: the fidelity channel gets the same credential screen."""
        result = node.execute(
            {
                "user_input": "see attached",
                "input_context": {"document": "key sk-" + "A" * 30 + " inside"},
            }
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert not result.get("document_text")


# ---------------------------------------------------------------------------
# DetectSourceLanguageNode — direction resolution
# ---------------------------------------------------------------------------


class TestDetectSourceLanguageNode:
    """TC-L01 – TC-L07: DetectSourceLanguageNode direction rules."""

    @pytest.fixture
    def node(self):
        from src.nodes.detect_source_language_node import DetectSourceLanguageNode

        return DetectSourceLanguageNode()

    def test_empty_document_returns_error(self, node):
        """TC-L01: empty document text → ERROR."""
        result = node.execute({"validated_input": ""})
        assert result["status"] == AgentStatus.ERROR.value

    def test_english_text_detected(self, node):
        """TC-L02: ASCII-dominant text → source=EN, target=JA."""
        result = node.execute(
            {"validated_input": "This contract sets out the terms and conditions between the parties."}
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["source_language"] == "EN"
        assert result["target_language"] == "JA"

    def test_japanese_text_detected(self, node):
        """TC-L03: CJK-dominant text → source=JA, target=EN."""
        result = node.execute(
            {"validated_input": "この契約書は当事者間の条件を定めるものです。契約条件は以下の通りです。"}
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["source_language"] == "JA"
        assert result["target_language"] == "EN"

    def test_target_is_opposite_of_source(self, node):
        """TC-L04: source and target are always opposite."""
        for text, expected_source in [
            ("Annual report and financial statement of FY2024.", "EN"),
            ("年度報告書および財務諸表（2024年度）。", "JA"),
        ]:
            result = node.execute({"validated_input": text})
            assert result["status"] == AgentStatus.SUCCESS.value
            expected_target = "JA" if expected_source == "EN" else "EN"
            assert result["source_language"] == expected_source
            assert result["target_language"] == expected_target

    def test_document_text_field_preferred(self, node):
        """TC-L05: document_text (the fidelity channel) wins over fallbacks."""
        result = node.execute(
            {"document_text": "この契約書は条件を定めます。", "validated_input": "english fallback text"}
        )
        assert result["source_language"] == "JA"

    def test_caller_pinned_target_fixes_direction(self, node):
        """TC-L06: a validated target_language pin overrides detection."""
        result = node.execute(
            {
                "validated_input": "This is clearly English prose.",
                "caller_request": json.dumps({"target_language": "EN"}),
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["target_language"] == "EN"
        assert result["source_language"] == "JA"

    def test_configured_threshold_reaches_detection(self, node):
        """TC-L07: the declared cjk_threshold changes the detection outcome."""
        # ~33% CJK characters: JA at the 0.05 floor, EN at a 0.9 threshold.
        mixed = "契約 terms and conditions 条項 liability 責任"
        low = node.execute({"validated_input": mixed})
        assert low["source_language"] == "JA"
        high = node.execute(
            {
                "validated_input": mixed,
                "runtime_settings": json.dumps({"cjk_threshold": 0.9}),
            }
        )
        assert high["source_language"] == "EN"

    @pytest.mark.parametrize(
        "bad_threshold",
        ["NaN", float("nan"), float("inf"), float("-inf"), True, -0.5, 1.5, "0.5"],
    )
    def test_out_of_contract_threshold_falls_back_to_floor(self, node, bad_threshold):
        """TC-L07b: a non-finite / out-of-range threshold never disables detection."""
        # json.dumps emits bare NaN/Infinity tokens and json.loads parses them —
        # which is exactly how such values arrive in a real checkpoint.
        result = node.execute(
            {
                "validated_input": "This is clearly English prose about contracts.",
                "runtime_settings": json.dumps({"cjk_threshold": bad_threshold}),
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["source_language"] == "EN"


# ---------------------------------------------------------------------------
# ClassifyDocumentTypeNode — document taxonomy + glossary producer
# ---------------------------------------------------------------------------


class TestClassifyDocumentTypeNode:
    """TC-C01 – TC-C08: classification, pinning, and glossary assembly."""

    @pytest.fixture
    def node(self):
        from src.nodes.classify_document_type_node import ClassifyDocumentTypeNode

        return ClassifyDocumentTypeNode()

    def test_empty_input_returns_error(self, node):
        """TC-C01: empty document text → ERROR.

        Stays terminal. The document reaches this node only after the outer
        pre_process published it, so an empty one here is a broken invariant
        inside the pipeline, not a value the caller can correct.
        """
        result = node.execute({"validated_input": ""})
        assert result["status"] == AgentStatus.ERROR.value

    def test_contract_classification(self, node):
        """TC-C02: contract-keyword text → document_type=contract."""
        result = node.execute(
            {
                "validated_input": (
                    "This agreement sets forth the terms and conditions, including indemnification "
                    "clauses and liability limits between the contracting parties."
                )
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["document_type"] == "contract"

    def test_proposal_classification(self, node):
        """TC-C03: proposal-keyword text → document_type=proposal."""
        result = node.execute(
            {
                "validated_input": (
                    "This proposal outlines the scope of work, deliverables, milestones, and "
                    "engagement approach for the consulting assignment."
                )
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["document_type"] == "proposal"

    def test_regulatory_filing_classification(self, node):
        """TC-C04: regulatory-keyword text → document_type=regulatory_filing."""
        result = node.execute(
            {
                "validated_input": (
                    "Annual disclosure filing under J-GAAP and IFRS standards; "
                    "financial statement includes audit opinion and going concern note."
                )
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["document_type"] == "regulatory_filing"

    def test_glossary_json_is_valid(self, node):
        """TC-C05: terminology_glossary_json is a valid JSON-encoded dict."""
        result = node.execute({"validated_input": "This contract agreement sets indemnification liability limits."})
        assert result["status"] == AgentStatus.SUCCESS.value
        glossary_json = result.get("terminology_glossary_json", "")
        assert isinstance(glossary_json, str), "glossary must travel as a JSON string"
        parsed = json.loads(glossary_json)
        assert isinstance(parsed, dict), "deserialized value must be a dict"
        assert len(parsed) > 0, "contract glossary must not be empty"

    def test_no_keyword_fallback_to_client_report(self, node):
        """TC-C06: unrecognised text → fallback document_type=client_report."""
        result = node.execute({"validated_input": "The quick brown fox jumps over the lazy dog."})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["document_type"] == "client_report"

    def test_caller_pinned_type_wins_over_heuristic(self, node):
        """TC-C07: a validated document_type pin overrides keyword scoring."""
        result = node.execute(
            {
                "validated_input": "This agreement covers indemnification and liability.",
                "caller_request": json.dumps({"document_type": "proposal"}),
            }
        )
        assert result["document_type"] == "proposal"
        glossary = json.loads(result["terminology_glossary_json"])
        assert "scope of work" in glossary

    def test_caller_glossary_merges_and_wins(self, node):
        """TC-C08: validated caller glossary_terms merge over the built-in set."""
        result = node.execute(
            {
                "validated_input": "This agreement covers indemnification and liability.",
                "caller_request": json.dumps(
                    {"glossary_terms": {"liability": "賠償責任", "Master Service Agreement": "基本業務委託契約"}}
                ),
            }
        )
        glossary = json.loads(result["terminology_glossary_json"])
        assert glossary["liability"] == "賠償責任", "caller's preferred translation wins"
        assert glossary["Master Service Agreement"] == "基本業務委託契約"
        assert glossary["indemnification"] == "損害賠償", "built-in terms are kept"


# ---------------------------------------------------------------------------
# TranslateWithDomainTermsNode — translation + glossary consumer + audit
# ---------------------------------------------------------------------------


class TestTranslateWithDomainTermsNode:
    """TC-T01 – TC-T08: translation logic, fidelity, and audit."""

    @pytest.fixture
    def node(self):
        from src.nodes.translate_with_domain_terms_node import TranslateWithDomainTermsNode

        return TranslateWithDomainTermsNode()

    def _contract_state(self, text: str = "This contract sets the liability limits.") -> dict:
        glossary = {"liability": "責任", "contract": "契約", "indemnification": "損害賠償"}
        return {
            "validated_input": text,
            "source_language": "EN",
            "target_language": "JA",
            "document_type": "contract",
            "terminology_glossary_json": json.dumps(glossary, ensure_ascii=False),
        }

    def test_empty_input_returns_error(self, node):
        """TC-T01: empty document text → ERROR.

        Stays terminal, for the same reason as TC-C01: an empty document at
        this depth means an upstream node's result went missing, which no
        corrected request would fix.
        """
        result = node.execute({**self._contract_state(), "validated_input": ""})
        assert result["status"] == AgentStatus.ERROR.value

    def test_translation_prefix_applied(self, node):
        """TC-T02: translation direction tag prepended to output."""
        result = node.execute(self._contract_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["translated_output"].startswith("[EN→JA]")

    def test_glossary_terms_substituted(self, node):
        """TC-T03: glossary terms are applied to the output."""
        state = self._contract_state("The liability is covered by indemnification.")
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        output = result["translated_output"]
        assert "責任" in output and "損害賠償" in output, f"Expected glossary substitutions in output; got: {output!r}"

    def test_ja_to_en_direction_tag(self, node):
        """TC-T04: JA source → [JA→EN] prefix."""
        state = {
            "validated_input": "この契約書は損害賠償の条件を定めます。",
            "source_language": "JA",
            "target_language": "EN",
            "document_type": "contract",
            "terminology_glossary_json": "{}",
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["translated_output"].startswith("[JA→EN]")

    def test_invalid_glossary_json_falls_back_to_empty(self, node):
        """TC-T05: malformed terminology_glossary_json → graceful fallback to empty glossary."""
        state = {**self._contract_state(), "terminology_glossary_json": "{not valid json"}
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["translated_output"].startswith("[EN→JA]")

    def test_document_text_field_preferred(self, node):
        """TC-T06: document_text (the fidelity channel) wins over fallbacks."""
        state = {**self._contract_state(), "document_text": "Only the liability clause."}
        result = node.execute(state)
        assert result["translated_output"] == "[EN→JA] Only the 責任 clause."

    @pytest.mark.parametrize(
        "value",
        ["8.512345", "9999.99999%", "ratio 0.123456", "JPY 1,234.56", "JPY 1234.56m", "ENG-2026-00123", "sku_48210"],
    )
    def test_numbers_and_identifiers_survive_byte_identical(self, node, value):
        """TC-T07: fidelity — figures and reference codes are never rewritten.

        A translation that alters a number falsifies the document, so no
        rounding, grouping or numeric normalisation may exist on this path.
        """
        state = self._contract_state(f"The agreed value is {value} as stated.")
        result = node.execute(state)
        assert value in result["translated_output"]

    def test_audit_event_emitted(self, node, monkeypatch):
        """TC-T08: emit_trace_event is called with correct event name and metadata."""
        calls: list[tuple] = []
        monkeypatch.setattr(
            "src.nodes.translate_with_domain_terms_node.emit_trace_event",
            lambda *a, **k: calls.append(a),
        )
        result = node.execute(self._contract_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert len(calls) == 1, f"Expected 1 audit call, got: {len(calls)}"
        event_name, payload, _state = calls[0]
        assert event_name == "translation_audit"
        assert payload.get("source_language") == "EN"
        assert payload.get("target_language") == "JA"
        assert "document_type" in payload
        assert "character_count" in payload

    def test_audit_payload_contains_no_document_content(self, node, monkeypatch):
        """TC-T09: the audit payload must not contain the document text itself.

        Assert on call.args[1] (the payload), NOT the full repr of the call
        (which would include the state arg carrying the raw input).
        """
        captured: list[tuple] = []
        monkeypatch.setattr(
            "src.nodes.translate_with_domain_terms_node.emit_trace_event",
            lambda *a, **k: captured.append(a),
        )
        secret_text = "CONFIDENTIAL_PROFESSIONAL_SERVICES_DOCUMENT"
        state = {**self._contract_state(secret_text)}
        node.execute(state)
        payload_repr = repr([c[1] for c in captured if len(c) > 1])
        assert secret_text not in payload_repr, f"document content found in audit payload: {payload_repr!r}"


# ---------------------------------------------------------------------------
# SecurityGateOutputNode — output gate
# ---------------------------------------------------------------------------


from src.nodes.security_gate_output_node import (  # noqa: E402
    _OUTPUT_BEARING_FIELDS,
    _SCREENED_FIELD,
    _WITHHELD_NOTICE,
)


class TestSecurityGateOutputNode:
    """TC-G01 – TC-G07: output gate."""

    @pytest.fixture
    def node(self):
        from src.nodes.security_gate_output_node import SecurityGateOutputNode

        return SecurityGateOutputNode()

    def _state(self, translated: str = "[EN→JA] The contract is executed.") -> dict:
        return {
            "translated_output": translated,
            "document_type": "contract",
            "source_language": "EN",
            "target_language": "JA",
        }

    def test_normal_output_passes(self, node):
        """TC-G01: clean output under size limit → SUCCESS + formatted_output set."""
        result = node.execute(self._state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result.get("formatted_output") == "[EN→JA] The contract is executed."

    def test_oversized_output_blocked(self, node, monkeypatch):
        """TC-G02: output > 200 000 chars → ERROR (size ceiling), and CONTAINED.

        The size verdict is asserted on the audit event rather than on
        error_log: the caller-visible message names the screened field only, so
        that the returned delta carries nothing the framework's own output scan
        could raise on (a raise there replaces the whole delta — the clearing
        with it — and the refused document survives).
        """
        calls: list[tuple] = []
        monkeypatch.setattr(
            "src.nodes.security_gate_output_node.emit_trace_event",
            lambda *a, **k: calls.append(a),
        )
        big = "[EN→JA] " + "a" * 200_001
        result = node.execute(self._state(big))
        assert result["status"] == AgentStatus.ERROR.value

        reasons = [c[1].get("reason", "") for c in calls if c[0] == "s3_gate_fail"]
        assert any("size" in r.lower() or "exceeds" in r.lower() for r in reasons), reasons
        assert result["error_log"] == [f"SecurityGateOutputNode: {_SCREENED_FIELD} failed the output gate"]
        assert result["formatted_output"] == _WITHHELD_NOTICE
        for field in _OUTPUT_BEARING_FIELDS:
            assert result[field] is None

    def test_credential_leak_blocked(self, node, monkeypatch):
        """TC-G03: credential pattern in translated output → ERROR, and CONTAINED.

        As with TC-G02 the verdict lives on the audit event. Here the reason for
        that split is at its sharpest: naming the matched credential in
        error_log would put a credential pattern INTO the returned delta, where
        the framework's own scan raises and substitutes a generic error — losing
        the clearing and shipping the leaking translation after all.
        """
        calls: list[tuple] = []
        monkeypatch.setattr(
            "src.nodes.security_gate_output_node.emit_trace_event",
            lambda *a, **k: calls.append(a),
        )
        leaked = "[EN→JA] The key is sk-" + "A" * 30
        result = node.execute(self._state(leaked))
        assert result["status"] == AgentStatus.ERROR.value

        reasons = [c[1].get("reason", "") for c in calls if c[0] == "s3_gate_fail"]
        assert any("credential" in r.lower() for r in reasons), reasons
        assert result["error_log"] == [f"SecurityGateOutputNode: {_SCREENED_FIELD} failed the output gate"]
        assert leaked not in str(result)
        for field in _OUTPUT_BEARING_FIELDS:
            assert result[field] is None

    def test_empty_translated_output_passes(self, node):
        """TC-G04: empty translated_output is not a credential/size violation → SUCCESS."""
        result = node.execute(self._state(""))
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_gate_pass_event_emitted(self, node, monkeypatch):
        """TC-G05: s3_gate_pass audit event emitted on success."""
        calls: list[tuple] = []
        monkeypatch.setattr(
            "src.nodes.security_gate_output_node.emit_trace_event",
            lambda *a, **k: calls.append(a),
        )
        node.execute(self._state())
        assert any(c[0] == "s3_gate_pass" for c in calls), f"Expected s3_gate_pass event; got: {[c[0] for c in calls]}"

    def test_gate_fail_event_emitted_on_block(self, node, monkeypatch):
        """TC-G06: s3_gate_fail audit event emitted when output is blocked."""
        calls: list[tuple] = []
        monkeypatch.setattr(
            "src.nodes.security_gate_output_node.emit_trace_event",
            lambda *a, **k: calls.append(a),
        )
        leaked = "[EN→JA] sk-" + "B" * 30
        node.execute(self._state(leaked))
        assert any(c[0] == "s3_gate_fail" for c in calls), f"Expected s3_gate_fail event; got: {[c[0] for c in calls]}"

    def test_pass_payload_has_no_content(self, node, monkeypatch):
        """TC-G07: pass payload must not include the translated text itself."""
        captured: list[tuple] = []
        monkeypatch.setattr(
            "src.nodes.security_gate_output_node.emit_trace_event",
            lambda *a, **k: captured.append(a),
        )
        secret_output = "CLASSIFIED_TRANSLATION_OUTPUT"
        node.execute(self._state(f"[EN→JA] {secret_output}"))
        payload_repr = repr([c[1] for c in captured if len(c) > 1])
        assert (
            secret_output not in payload_repr
        ), f"audit payload must not include translated content; got: {payload_repr!r}"
