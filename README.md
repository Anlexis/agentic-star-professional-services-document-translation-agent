# Professional Services Document Translation Agent

AI agent for translating professional services documents between English and Japanese, built with Agentic Star.

> **Category**: Cat 2 (a domain pipeline: a translation workflow behind the fixed agent backbone)
> **Industry**: Services
> **Template ID**: SVC-C2-008

## Overview

Translates professional-services documents — contracts, proposals, regulatory filings and client
reports — between English and Japanese while keeping the domain terminology consistent: legal
contract language, IFRS/J-GAAP accounting vocabulary and consulting-engagement terms are resolved
through a per-document-type glossary rather than left to a generic translator. The output carries a
direction tag (`[EN→JA]` / `[JA→EN]`) and, outside glossary substitutions, reproduces the document
byte for byte — figures, reference codes and dates are never reformatted.

A caller may also submit the engagement's own terminology (term → preferred translation) alongside
the document, plus a document-type or direction pin. Those travel on a separate structured channel
for a concrete reason: professional-services terminology is written as consecutive capitalised
words ("Master Service Agreement", "Change Management"), which is the shape the platform's input
screen masks on the plain input field — a document sent there arrives with such phrases replaced by
mask tokens. The structured channel's `document` field is delivered verbatim and screened by this
template instead, and a document or term carrying a contact identifier is refused rather than
masked.

Detection, classification and the translation step are deterministic and offline: the direction
comes from a character-set heuristic, the type from keyword scoring, and the shipped translation
stage applies the glossary as literal substitutions, so the same document always returns the same
output. Swapping in a model-backed translation service is a change at one clearly marked call
site, not a rewrite.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at graph
compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

Run the walkthrough demo (compiles the real agent, translates a sample contract):

```bash
python -m src.examples.graph_cat2_sample
```

## Configuration

| File | Role |
|---|---|
| `config/agent.yaml` | Static registration manifest — identity only, read at root level by the registry. |
| `config/config.yaml` | Every runtime parameter: `max_retry`, the language-detection threshold (`detection.cjk_threshold`) and the caller-glossary entry cap (`translation.max_caller_glossary_terms`). Loaded by the registry and by `src/api/server.py`, so both deployments run on the same declaration. |

## Project Structure

```
src/          agent implementation (graphs, nodes, schemas, HTTP entry point)
tests/        unit, caller-contract, config and end-to-end boundary tests
config/       agent manifest and runtime parameters
docs/         design and test documentation
```

See `docs/02_design.md` for the architecture, the caller-data contract and the security
boundaries, and `docs/03_test_spec.md` for what the suite covers.

## Customising

1. Adjust `config/config.yaml` for your own environment and policies.
2. Extend the per-document-type glossaries in `src/nodes/classify_document_type_node.py`
   with your own terminology.
3. Replace the `_apply_glossary` call site in
   `src/nodes/translate_with_domain_terms_node.py` with your translation backend.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
