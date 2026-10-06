# docs/02_design.md — SVC-C2-008 Design

## Template Metadata

| Field | Value |
|---|---|
| Template ID | SVC-C2-008 |
| Category | Cat 2 |
| Industry | SVC (Professional Services) |
| L1 Base (framework base class) | AgentBaseGraph — direct framework inheritance |
| Pattern | Nested domain pipeline (outer backbone + inner workflow) |

## Position in the AgentCore Architecture

- **Agent Class**: `ProfServicesTranslationAgent(AgentBaseGraph)`
- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic — msgpack incompatible)
  - Node: `execute(self, state: dict) -> dict`, partial-dict returns
  - Graph: Cat 2 nested (outer AgentBaseGraph + inner BaseGraph via GraphNode)

## Architecture — Cat 2 Nested Pattern

```
Outer graph (AgentBaseGraph — graph.py):
  START → initialize → pre_process → main → post_process → finalize → END
                           │           │           │
                  ValidateInputNode  TranslationWorkflowGraphNode  SecurityGateOutputNode
                  (VERIFIED_EXTERNAL)  (delegates to inner graph)

Inner graph (BaseGraph — domain_workflow_graph.py,
             via TranslationWorkflowGraphNode.get_subgraph()):
  START → detect_source_language → classify_document_type
        → translate_with_domain_terms → END
```

### Outer Graph (`src/graph/graph.py`)

- Class: `ProfServicesTranslationAgent(AgentBaseGraph)`
- Backbone slots:
  - `pre_process`: `ValidateInputNode` — trust gate + caller-data contract + document screen
  - `main`: `TranslationWorkflowGraphNode` — GraphNode wrapping `DomainWorkflowGraph`
  - `post_process`: `SecurityGateOutputNode` — output gate

### Inner Graph (`src/graph/domain_workflow_graph.py`)

- Class: `DomainWorkflowGraph(BaseGraph)`
- All 7 ABC methods implemented: `name`, `state_schema`, `_validate_config`,
  `register_nodes`, `add_edges`, `route`, `get_output`
- Node instantiation: no constructor arguments; config reaches nodes through State

## Runtime Configuration

`config/agent.yaml` is the static registration manifest (identity only; every key at
root level). `config/config.yaml` holds every runtime parameter; the registry loads it
and passes it as `Graph(config=...)`, and `src/api/server.py` does the same through
`_runtime_config()`, so a registry-loaded agent and a standalone deployment run on the
same declaration.

| Key | Consumer | Effect |
|---|---|---|
| `max_retry` | AgentBaseGraph (framework) | Retry-routing ceiling on the backbone |
| `timeout_s` | Platform registry | Invocation timeout budget |
| `detection.cjk_threshold` | DetectSourceLanguageNode (inner graph) | CJK fraction at which a document is detected as Japanese |
| `translation.max_caller_glossary_terms` | ValidateInputNode | Entry cap on the caller glossary |

Every declared value is validated once in `declared_settings()` (type, finiteness,
range — a NaN threshold would compare False against every bound and silently disable
detection, so non-finite values are rejected, and the consumer keeps its built-in
floor). The validated settings reach the inner graph through
`TranslationWorkflowGraphNode._parent_config()` →
`DomainWorkflowGraph._extra_initial_state()` → the `runtime_settings` state field, and
reach the pre_process node through the outer `_extra_initial_state()`. The
end-to-end tests prove arrival by flipping the declaration and reading the change off
the rendered output.

## Caller-Data Contract

`POST /invoke` accepts `input` (plain text), `session_id`, and the structured
`input_context` channel (adapter-capped at 256 KB serialized). Every structured field
is validated in `ValidateInputNode`; violations fail CLOSED with the field named and
the value never echoed. How a violation is REPORTED depends on which kind it is —
see *Two ways a request stops* below.

| Field | Contract |
|---|---|
| `document` | The document on the fidelity channel: string, non-empty, ≤100 000 chars. Delivered to the pipeline verbatim. |
| `document_type` | Slug pin: `contract` \| `proposal` \| `regulatory_filing` \| `client_report`. Overrides keyword classification. |
| `target_language` | Slug pin: `en` \| `ja`. Fixes the translation direction (the source is the other language of the pair). |
| `glossary_terms` | Mapping term → preferred translation. Entry-capped by the declared setting; each entry ≤80 chars of the term alphabet (word characters in any script plus space and `. , ( ) / & + ' -`). Caller entries win over the built-in glossary on a shared term. |

### The two input channels

The platform's input screen masks contact identifiers AND any run of two or more
consecutive capitalised words on the plain `input` field before the agent code runs.
That heuristic's shape is exactly professional-services terminology — a service line
or agreement name is capitalised words ("Master Service Agreement", "Change
Management") — so a document sent as `input` reaches the pipeline with such phrases
replaced by unrecoverable mask tokens.

`input_context.document` is therefore the fidelity channel: the framework delivers
structured fields verbatim, the document crosses the outer→inner graph boundary on a
domain state field (`document_text`, carried by `src/graph/context_bridge.py`) that
the platform screen does not rewrite, and **this template screens the channel
itself**:

- credential material (API keys, bearer tokens, cloud access keys, private-key
  blocks) — refused on both channels;
- instruction-override phrasings and chat-template control tokens (`<|…|>`,
  `[INST]`, `<<SYS>>`, role tags) — refused on both channels, applied post-parse,
  field NAMES included; a hostile key is refused by position and never echoed;
- contact identifiers (email, phone, national/payment IDs via the platform's
  high-precision detector types) — REFUSED on the fidelity channel rather than
  masked, with the error pointing the caller at the default channel where the
  platform masks them. The personal-name heuristic is deliberately NOT part of this
  screen: two capitalised words is what a service line IS, and screening on that
  type would refuse every real document.

There is no path on which unvalidated caller data reaches the pipeline, and no
stub-only public path: the pipeline computes its output from the validated caller
document on every run.

### Two ways a request stops

Both stop the request in exactly the same way — nothing is translated, nothing is
published, and the audit message naming the field goes to `error_log` — and they
differ only in how the stop is reported.

- **A bound the caller can correct** — an empty, oversized or non-string document,
  an unknown `document_type` or `target_language`, a malformed or over-capped
  `glossary_terms` mapping, a glossary entry outside the term alphabet or length
  limit or carrying a client/engagement identifier, a non-object `input_context`,
  a non-string field NAME — **completes** the run (`status =
  AgentStatus.SUCCESS.value`) carrying a reason code in `error_code`, and the
  caller-facing body is that code's fixed sentence. The caller reads what to
  correct and sends the request again on the same conversation. Terminating
  instead would end the calling surface's turn and surface only an exception type,
  leaving the reason reachable solely from the audit trail.
- **Content the template refuses** — credential material, instruction-override or
  chat-template control text (in a value or in a field name), and a contact
  identifier on the fidelity channel — **terminates** (`status =
  AgentStatus.ERROR.value`). A refusal is not a value to correct, and reporting it
  as one would read as an invitation to reword the payload until it is accepted.

The class travels as a return value from each validator, never recovered from the
message text: the messages are written for the audit log and change freely, and a
refusal silently re-classified by an edited sentence is the failure this split
exists to make impossible. The reason code is internal — it marks State so the
main slot skips the inner graph and each inner node returns without acting, it
crosses the inner/outer boundary with the outer reason winning, and it is never
surfaced in the response envelope.

## Output Contract

The external output is the translated document: a direction tag (`[EN→JA]` /
`[JA→EN]`) followed by the document text with glossary substitutions applied.

**Numeric precision decision — no rounding grid applies at this boundary.** This
template renders no monetary aggregate and computes no numeric report: its output is
a translation of the caller's own document, returned to that caller. Rewriting a
figure inside a translated contract (rounding, re-grouping, snapping) would falsify
the document — the opposite of this template's contract. The published invariant is
therefore **byte fidelity**: outside glossary-term substitution, the document body is
emitted byte for byte. This is enforced by construction (no numeric post-processing
exists on the path) and pinned both ways in the test suite: decimals
(`8.512345`, `9999.99999%`, `JPY 1,234.56`, `JPY 1234.56m`), reference codes
(`ENG-2026-00123`, `sku_48210`) and embedded acronym-number pairs (`STAR 2026`)
arrive byte-identical through the real `/invoke` path.

The output gate (`SecurityGateOutputNode`) REFUSES rather than rewrites: translated
output above 200 000 chars or carrying a credential pattern yields `status=error`
and no translated document, with a `s3_gate_fail` audit event.

Refusing and withholding are two separate pieces of work, and the gate does both.
By the time it runs, the main slot has already mapped the translated document into
the outer state — `TranslationWorkflowGraphNode.merge_output()` writes it to both
`translated_output` and `result` — and the released text is resolved as
`formatted_output or result`. So an error status on its own would still hand the
caller the document that was just refused. On a violation the gate therefore clears
every field carrying a representation of the document and substitutes a non-empty
withheld notice for `formatted_output`. The notice is deliberately non-empty: an
empty string is falsy, so `"" or result` is `result`, and emptying the field
re-opens the very path it appears to close.

The refusal names the screened FIELD and nothing else. The size or credential
verdict goes to the operator log and the `s3_gate_fail` audit payload, which are not
caller-visible. Quoting the offending text in the returned delta would place it
where the framework's own output scan reads it — that scan raises, the node wrapper
replaces the whole delta with a generic error, and the clearing is discarded along
with it.

## Node Design

| Node | File | Slot / Graph | Trust Level |
|---|---|---|---|
| ValidateInputNode | validate_input_node.py | pre_process (outer) | VERIFIED_EXTERNAL |
| TranslationWorkflowGraphNode | graph.py | main (outer) | — (GraphNode wrapper) |
| DetectSourceLanguageNode | detect_source_language_node.py | detect_source_language (inner) | ANONYMOUS |
| ClassifyDocumentTypeNode | classify_document_type_node.py | classify_document_type (inner) | ANONYMOUS |
| TranslateWithDomainTermsNode | translate_with_domain_terms_node.py | translate_with_domain_terms (inner) | ANONYMOUS |
| SecurityGateOutputNode | security_gate_output_node.py | post_process (outer) | ANONYMOUS |

The outer pre_process slot is the external trust boundary (VERIFIED_EXTERNAL); the
inner nodes and the output gate run behind it and admit the already-validated caller
(the sub-graph inherits the outer invocation context unchanged — no trust
escalation).

### ValidateInputNode

- Owns the caller-data contract above; publishes `validated_input`, `document_text`
  and the JSON-encoded `caller_request`
- A correctable bound completes the run carrying `error_code`; refused content
  terminates (see *Two ways a request stops*). The branch is decided by the
  validator's returned code, not by the node
- Reads the caller-glossary entry cap from the `runtime_settings` state field

### DetectSourceLanguageNode

- Caller `target_language` pin fixes the direction; otherwise CJK character-fraction
  heuristic at the declared `cjk_threshold` (built-in floor 0.05)
- Sets `source_language` / `target_language` ("EN" | "JA")

### ClassifyDocumentTypeNode

- Caller `document_type` pin wins; otherwise keyword-scored classification into:
  `contract`, `proposal`, `regulatory_filing`, `client_report`
- Merges the built-in glossary for the resolved type with the caller's validated
  `glossary_terms` (caller wins on a shared term); serializes the merge as
  `terminology_glossary_json`

### TranslateWithDomainTermsNode

- Deserializes `terminology_glossary_json`; applies substitutions longest-match-first
- Shipped translation stage is deterministic (glossary substitution + direction
  tag); a production deployment replaces the single `_apply_glossary` call site with
  a model-backed translation service
- Emits the `translation_audit` event with provenance metadata only — never document
  content (NDA material)

### SecurityGateOutputNode

- Module-level `_security_gate_output(output, state)` function (never a framework
  hook): size ceiling (200 000 chars) + credential-leak scan
- Emits `s3_gate_pass` / `s3_gate_fail` audit events
- On a violation, CONTAINS as well as refuses: clears `_OUTPUT_BEARING_FIELDS`
  (`result`, `translated_output`) and sets `formatted_output` to a non-empty
  withheld notice, so the base envelope's `formatted_output or result` resolution
  cannot fall back to the refused document. `document_type`, `source_language` and
  `target_language` are inert provenance enumerations and are left in place so the
  refusal stays diagnosable
- The caller-visible error names the screened field only; the verdict itself travels
  on the operator log and the audit event
- On a run declined upstream (`error_code` set), it clears the same
  document-bearing fields — nothing was translated, since the main slot never ran
  the inner graph — renders that code's fixed sentence as the truthy
  `formatted_output`, and completes carrying the marker

## State Schema (`src/schemas/state.py`)

```python
class State(AgentState):
    validated_input: Optional[str]            # accepted document text
    document_text: Optional[str]              # fidelity channel (never rewritten by the platform screen)
    caller_request: Optional[str]             # JSON: validated pins + glossary_terms
    runtime_settings: Optional[str]           # JSON: validated config declarations
    source_language: Optional[str]            # "EN" | "JA"
    target_language: Optional[str]            # "EN" | "JA"
    document_type: Optional[str]              # contract | proposal | regulatory_filing | client_report
    terminology_glossary_json: Optional[str]  # JSON: merged term → translation mapping
    translated_output: Optional[str]          # final translated document
```

All fields are `Optional[str]`; dict-valued data travels JSON-encoded (`json.dumps()`
on write, `json.loads()` on read) to stay flat and msgpack-safe across checkpoints.

## Security Controls

| Control | Node | Implementation |
|---|---|---|
| Caller trust gate | ValidateInputNode | `required_trust_level = VERIFIED_EXTERNAL` (framework-enforced) |
| Input validation | ValidateInputNode | Bounds, credential screen, injection/control-token screen, contact-identifier screen — in `execute()`, owned by the template (proven by direct-`execute()` tests, no framework wrapper in front). A bound completes carrying `error_code`; a screen terminates |
| Output gate | SecurityGateOutputNode | Module-level `_security_gate_output()` |
| Audit logging | TranslateWithDomainTermsNode, SecurityGateOutputNode | `emit_trace_event()` — metadata only, never content |
| Entry-point auth | src/api/server.py | Bearer token (`INVOKE_AUTH_TOKEN`) → VERIFIED_EXTERNAL; middleware-established trust never demoted |

## GraphNode Contract

- `TranslationWorkflowGraphNode.get_subgraph()` returns
  `DomainWorkflowGraph(config=self._parent_config())`
- `extract_input()` stashes `document_text` + `caller_request` on the context bridge
  (the framework does not forward outer state fields into a subgraph) and returns the
  validated document string
- `merge_output()` maps `translated_output`, `document_type`, `source_language`,
  `target_language`, `result`, `status` from sub_result, plus `error_code` — with
  the outer reason winning, since a reason settled before the inner run is the
  real one and a plain `sub_result.get()` would erase it
- `DomainWorkflowGraph.get_output()` provides all fields merge_output reads
  (designed together)

## Import Isolation Confirmation

- Template does not import the platform-internal SDK layer
- Import targets: `framework.*` and `shared.utils.audit_logger` only

## Design Decision Record

| Decision | Chosen | Rationale |
|---|---|---|
| Base class | AgentBaseGraph | Cat 2 multi-step domain workflow (nested pattern) |
| Graph topology | Nested (outer + inner) | 3 domain steps do not warrant an autonomous loop |
| Composition pattern | GraphNode (subgraph) | Encapsulate domain steps in inner BaseGraph |
| Error propagation | propagate | Fail-fast; translation errors must be surfaced |
| Document channel | `input_context.document` + context bridge | The platform screen masks capitalised terminology on `user_input`; the fidelity channel preserves it and is screened by the template (refusing, not masking) |
| Numeric precision | Byte fidelity, no rounding grid | Output is the caller's own document; altering a figure falsifies it (see Output Contract) |
| Output-gate style | Module-level function in execute() | Framework gate hooks are final; domain checks live in execute() |
| Glossary state | Optional[str] JSON | Dict serialized for msgpack checkpoint safety |
| Runtime config | config/config.yaml via `declared_settings()` | One validated declaration, live in both deployments; no dead keys |
