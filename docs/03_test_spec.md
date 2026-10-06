# SVC-C2-008 — Test Specification

**Template:** SVC-C2-008 Professional Services Document Translation Agent EN↔JA
**Category:** Cat 2 — domain-specific pipeline (nested graph pattern)

---

## 1. Scope

This document defines the test cases for the SVC-C2-008 agent, organized as:

- **Unit tests** (`tests/unit/test_agent.py`): per-node domain logic and audit behaviour
- **Caller-contract tests** (`tests/unit/test_caller_contract.py`): the structured-channel
  contract owned by `ValidateInputNode`, proven by direct `execute()` calls
- **Trust-gate tests** (`tests/unit/test_trust_gate.py`): framework trust enforcement through
  `node(state)` (`BaseNode.__call__`)
- **Config/manifest tests** (`tests/unit/test_config_manifest.py`): manifest identity and live
  runtime configuration
- **Framework-compliance tests** (`tests/unit/test_framework_compliance_tc06_tc07.py`)
- **Proof-of-boundary tests** (`tests/proof_of_boundary/`): hook ordering, backbone order,
  import isolation, state safety, server auth boundary, interrupt propagation, output-gate
  containment, and the end-to-end `/invoke` path

All domain test inputs are English or Japanese professional-services documents. No model is
called; the shipped translation stage applies glossary substitutions and prefixes the output
with a direction tag (`[EN→JA]` / `[JA→EN]`).

---

## 2. Test Environment

| Item | Requirement |
|---|---|
| Python | 3.11+ |
| Framework | `agenticstar-agentcore==1.0.1` (real wheel) |
| Test runner | `pytest` |
| Shared package | `shared.*` available from the wheel — NEVER stubbed via `sys.modules` |
| Audit patching | `emit_trace_event` patched at node module level (`monkeypatch.setattr("src.nodes.<mod>.emit_trace_event", ...)`) |

---

## 3. Test Cases — ValidateInputNode

This node stops a request two ways, and the tests assert them apart. A bound the
caller can correct is **declined**: the run completes (`status=SUCCESS`) carrying
a reason code in `error_code`, and both halves are asserted together — the status
alone would also pass on a run that quietly translated. Content the template
**refuses** (credential material, override/control text, a contact identifier on
the fidelity channel) terminates with `status=ERROR`. Nothing is published on
either path.

| TC-ID | Description | Input | Expected Result |
|---|---|---|---|
| TC-V01 | Empty document declined | `user_input=""` | status=SUCCESS + `error_code` set, "empty" in error_log |
| TC-V02 | Whitespace-only declined | `user_input="  \t\n"` | status=SUCCESS + `error_code` set |
| TC-V03 | Oversized document declined (> 100 000 chars) | 100 001-char string | status=SUCCESS + `error_code` set |
| TC-V04 | API key pattern blocked | `"translate: sk-AAAA...30"` | status=ERROR, "credential" in error_log |
| TC-V05 | Bearer token pattern blocked | `"Bearer xxx...30"` | status=ERROR |
| TC-V06 | Valid English contract text accepted | Valid contract sentence | status=SUCCESS, `validated_input` + `document_text` set |
| TC-V07 | Missing `user_input` key treated as empty | `{}` | status=SUCCESS + `error_code` set (declined) |
| TC-V08 | Fidelity document wins over user_input | `input_context.document` set | `document_text` = the fidelity document |
| TC-V09 | Fidelity channel screens credentials too | credential inside `document` | status=ERROR, nothing published |

The full structured-channel matrix (injection phrases, control tokens, escaped
payloads, hostile field names, contact identifiers, engagement identifiers, slug
enumerations, glossary alphabet/length/entry-cap bounds, and the pass-direction
probes on real contract language) lives in `tests/unit/test_caller_contract.py` —
every stop asserted behaviourally — a bound by the completing status plus its
reason code, a refusal by the error status — with nothing published, the field
named, and the value never echoed in both cases.

---

## 4. Test Cases — DetectSourceLanguageNode

| TC-ID | Description | Input | Expected Result |
|---|---|---|---|
| TC-L01 | Empty document text rejected | `validated_input=""` | status=ERROR |
| TC-L02 | ASCII-dominant text → EN | English contract sentence | source=EN, target=JA |
| TC-L03 | CJK-dominant text → JA | Japanese sentence | source=JA, target=EN |
| TC-L04 | source and target always opposite | EN and JA inputs | source ≠ target |
| TC-L05 | `document_text` preferred over fallbacks | JA `document_text` + EN `validated_input` | source=JA |
| TC-L06 | Caller target pin fixes the direction | `caller_request.target_language="EN"` | target=EN, source=JA |
| TC-L07 | Declared `cjk_threshold` changes detection | mixed doc at 0.05 vs 0.9 | JA vs EN |
| TC-L07b | Out-of-contract threshold falls back to floor | NaN/±Inf/bool/out-of-range/string | detection still runs at 0.05 |

---

## 5. Test Cases — ClassifyDocumentTypeNode

| TC-ID | Description | Input | Expected Result |
|---|---|---|---|
| TC-C01 | Empty document text rejected | `validated_input=""` | status=ERROR |
| TC-C02 | Contract keywords → contract | "agreement", "indemnification", ... | document_type=contract |
| TC-C03 | Proposal keywords → proposal | "scope of work", "milestones", ... | document_type=proposal |
| TC-C04 | Regulatory keywords → regulatory_filing | "J-GAAP", "IFRS", "audit", ... | document_type=regulatory_filing |
| TC-C05 | Glossary JSON is a valid encoded dict | any classified text | deserializes to non-empty dict |
| TC-C06 | No keywords → fallback client_report | generic text | document_type=client_report |
| TC-C07 | Caller pin wins over the heuristic | contract text + `document_type=proposal` pin | document_type=proposal, proposal glossary |
| TC-C08 | Caller glossary merges and wins | caller `glossary_terms` | caller translation wins; built-ins kept |

---

## 6. Test Cases — TranslateWithDomainTermsNode

| TC-ID | Description | Input | Expected Result |
|---|---|---|---|
| TC-T01 | Empty document text rejected | `validated_input=""` | status=ERROR |
| TC-T02 | Direction tag prepended | EN text | output starts `[EN→JA]` |
| TC-T03 | Glossary terms substituted | contract text + glossary | JA terms appear in output |
| TC-T04 | JA→EN direction tag | `source_language="JA"` | output starts `[JA→EN]` |
| TC-T05 | Malformed glossary JSON → empty fallback | `"{not valid"` | status=SUCCESS, tag preserved |
| TC-T06 | `document_text` preferred over fallbacks | distinct `document_text` | that text is translated |
| TC-T07 | Byte fidelity — figures and codes never rewritten | `8.512345`, `9999.99999%`, `JPY 1,234.56`, `JPY 1234.56m`, `ENG-2026-00123`, `sku_48210` | value appears byte-identical in output |
| TC-T08 | Audit event emitted | valid input | one `translation_audit` call with direction + counts |
| TC-T09 | Audit payload carries no document content | marker text | marker absent from payload repr |

---

## 7. Test Cases — SecurityGateOutputNode

| TC-ID | Description | Input | Expected Result |
|---|---|---|---|
| TC-G01 | Clean output passes | normal translated text | status=SUCCESS, `formatted_output` set |
| TC-G02 | Output > 200 000 chars blocked | 200 001-char output | status=ERROR |
| TC-G03 | Credential leak blocked | `sk-...` in output | status=ERROR |
| TC-G04 | Empty output passes | `""` | status=SUCCESS |
| TC-G05 | `s3_gate_pass` audit event on success | normal output | event emitted |
| TC-G06 | `s3_gate_fail` audit event on block | credential output | event emitted |
| TC-G07 | Pass payload carries no content | marker output | marker absent from payload repr |

---

## 8. Test Cases — Config / Manifest (`tests/unit/test_config_manifest.py`)

| TC-ID | Description | Expected Result |
|---|---|---|
| CFG-01 | Manifest is flat (no nested `agent:` block) | id / namespace read at root |
| CFG-02 | Declared class is the graph class | dotted path resolves |
| CFG-03 | Category / industry / base_type | Cat 2 / SVC / ChatAgent |
| CFG-04 | required_trust_level matches the entry gate | VERIFIED_EXTERNAL on ValidateInputNode |
| CFG-05 | max_retry within framework ceiling | integer in [0, 10) |
| CFG-06 | `_runtime_config()` reads the shipped file | equals config/config.yaml |
| CFG-07 | Declared settings reach the inner graph | forwarded configurable block matches the file |
| CFG-08 | Declared value wins over the module floor | changed declaration travels |
| — | Out-of-contract declarations fall back | NaN/Inf/bool/range/string → built-in floor |
| — | Outer graph seeds settings into state | `runtime_settings` carries the declared cap |

---

## 9. Proof-of-Boundary Tests

### PB — Import Isolation (`test_import_isolation.py`)

Verifies no platform-internal SDK imports exist in `src/`.

### PB-6 — Per-Node Hook Order (`test_pb_invoke_order.py::TestInvokeOrder`)

Verifies every concrete node in `src/nodes/` runs its `__call__` hooks in order:
```
event:node_start → security_gate_input → execute → security_gate_output → event:node_complete
```

Nodes covered: `ValidateInputNode`, `DetectSourceLanguageNode`,
`ClassifyDocumentTypeNode`, `TranslateWithDomainTermsNode`, `SecurityGateOutputNode`.

### PB-6b — Full Graph Backbone Order (`test_pb_invoke_order.py::TestGraphInvokeOrder`)

Invokes `ProfServicesTranslationAgent` end-to-end with `_VALID_PAYLOAD`:
`InitializeNode → ValidateInputNode → TranslationWorkflowGraphNode →
SecurityGateOutputNode → FinalizeNode` in order, final `status == SUCCESS`.

### PB-2 — State Safety (`test_state_safety.py`)

Verifies `State` carries no credential-like fields and no prohibited types.

### PB-7 — Interrupt Propagation (`test_pb7_hitl_interrupt_propagation.py`)

Skip stub: no graph class declares `propagate_hitl=True` (human-in-the-loop is not
enabled — see `config/config.yaml`).

### PB — Server Auth Boundary (`test_server_boot.py`)

The standalone entry point's bearer-token boundary, driven through the raw ASGI
interface: missing/wrong/malformed/non-ASCII tokens → generic 401; correct token →
VERIFIED_EXTERNAL; unconfigured token → ANONYMOUS, never elevated;
middleware-established trust never demoted.

### PB-OC — Output-Gate Containment (`test_pb_output_containment.py`)

Through the real ASGI `/invoke`, driven by the domain's own behaviour rather than a
patched screen: a caller glossary that expands a short recurring term pushes the
TRANSLATED document past the gate's size ceiling while the submitted document stays
inside its own bound.

1. **Premise guards** — the submitted document is within the input bound, the
   translated one clears the gate ceiling, and `SecurityGateOutputNode` is present in
   `node_history` on the blocked invoke. Without these the containment assertions
   below could pass while exercising nothing.
2. **The error envelope carries no translated document** — `status=error`, and the
   confidential marker, the glossary rendering and the document body are all absent;
   `output` is the withheld notice.
3. **No diagnostics leak** — no matched text, no traceback, no source paths.
4. **The falsy/truthy distinction is pinned** — an emptied `formatted_output` is shown
   to fall through to `result` (the refused document), and the shipped notice is shown
   not to.
5. **Every document-bearing field is cleared**, with an inventory guard asserting that
   anything `merge_output()` maps is either cleared or declared content-free — so a
   field added there cannot silently escape the clearing. `error_code` is declared
   content-free alongside `status`: it is a closed set of reason codes, never
   document text, and clearing it with the document-bearing fields would leave a
   caller a blank body with nothing saying why.
6. **Clean-path control** — same route, same glossary, same gate, size violation
   removed: a legitimate translation still reaches the caller in full with the caller's
   glossary term applied.

### PB-E2E — The Public Path (`test_invoke_e2e.py`)

Through the real ASGI `/invoke` with bearer auth:

1. **Auth boundary** — no token, no translation.
2. **Real work** — the pipeline computes a real translation from caller data (glossary
   applied, direction tag correct, both directions).
3. **Declared config arrives** — flipping `detection.cjk_threshold` in
   `config/config.yaml` flips the rendered direction tag; declaring
   `translation.max_caller_glossary_terms: 1` makes a two-term glossary stop the
   request — declined, so the envelope completes carrying the reason sentence and
   no translation.
4. **Caller data changes the answer** — the fidelity channel carries terminology the
   default channel masks (the same document via `input` arrives with mask tokens);
   caller glossary terms steer the translation; pinned type selects its glossary;
   pinned target fixes the direction.
5. **Validation rejection** — a parametrized violation matrix through `/invoke`
   yields `status=success` with the fixed reason sentence as the body and no
   direction tag anywhere in it (the reason code stays in state and never reaches
   the caller); an oversized channel is refused 413 at the adapter; hostile content
   terminates (`status=error`, no output) on either channel; a contact identifier on
   the fidelity channel terminates the same way and is never echoed.
6. **Fidelity contract** — decimals, percentages, monetary strings, reference codes
   and acronym-number pairs arrive byte-identical.

---

## 10. Security Coverage Summary

| Control | Test Coverage |
|---|---|
| Caller trust gate | `test_trust_gate.py` (denial + positive control + matrix) |
| Input validation (both channels) | TC-V01..09 + `test_caller_contract.py` |
| Injection / control-token screen (template-owned) | `test_caller_contract.py::TestInjectionScreening` (direct `execute()`, both directions) + E2E |
| Contact-identifier screen (fidelity channel) | `test_caller_contract.py::TestFidelityChannel` + E2E |
| Output gate (size + credential) | TC-G01..07 |
| Audit provenance (no content) | TC-T08/09, TC-G05..07 |
| Entry-point auth | `test_server_boot.py` + E2E auth boundary |
| Byte-fidelity output invariant | TC-T07 + E2E fidelity contract |
