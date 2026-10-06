# SVC-C2-008 — Unit Tests: manifest / runtime-config consistency
#
# Two files, two jobs, and both are live rather than documentation:
#   config/agent.yaml   the static registration manifest — identity only. The
#                       registry reads every key at ROOT level.
#   config/config.yaml  every runtime parameter. The registry loads it and passes
#                       it as Graph(config=...); src/api/server.py does the same.
#
# These tests pin the manifest against the code it names, and pin the runtime
# declaration against the settings the graph actually forwards — a drift in either
# direction fails here rather than degrading silently at runtime.
#
# Mirrors docs/03_test_spec.md §8 (CFG-01..CFG-08). Deterministic — no network.

import pathlib

import yaml

from framework.schemas.trust_level import TrustLevel

from src.graph.graph import (
    ProfServicesTranslationAgent,
    TranslationWorkflowGraphNode,
    _runtime_config,
    declared_settings,
)
from src.nodes.validate_input_node import ValidateInputNode

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MANIFEST = yaml.safe_load((_ROOT / "config" / "agent.yaml").read_text(encoding="utf-8"))
_CONFIG = yaml.safe_load((_ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))


class TestManifestIdentity:
    def test_cfg_01_manifest_is_flat(self):
        # The registry reads root-level keys; a nested `agent:` block would leave
        # every one of them unread.
        assert "agent" not in _MANIFEST
        assert _MANIFEST["id"] == "SVC-C2-008"
        assert _MANIFEST["namespace"] == "svc"

    def test_cfg_02_declared_class_is_the_graph_class(self):
        assert _MANIFEST["class"] == "src.graph.graph.ProfServicesTranslationAgent"
        assert _MANIFEST["name"] == "Professional Services Document Translation Agent"
        assert ProfServicesTranslationAgent().name == "ProfServicesTranslationAgent"

    def test_cfg_03_category_and_industry(self):
        assert _MANIFEST["category"] == "Cat 2"
        assert _MANIFEST["industry"] == "SVC"
        assert _MANIFEST["base_type"] == "ChatAgent"

    def test_cfg_declares_no_secrets_or_extras(self):
        # Declaring a secret or extra that is never required makes the agent fail
        # at compile time; this template calls no secrets provider and constructs
        # no model client, so both lists stay empty.
        assert _MANIFEST["requires"]["secrets"] == []
        assert _MANIFEST["requires"]["extras"] == []
        assert _MANIFEST["generation_mode"] == "deterministic"


class TestManifestSecurity:
    def test_cfg_04_required_trust_level_matches_the_entry_gate(self):
        declared = TrustLevel(_MANIFEST["required_trust_level"])
        assert declared is TrustLevel.VERIFIED_EXTERNAL
        assert ValidateInputNode.required_trust_level is declared

    def test_cfg_05_max_retry_within_framework_ceiling(self):
        max_retry = _CONFIG["max_retry"]
        assert isinstance(max_retry, int)
        assert 0 <= max_retry < 10  # the framework's retry ceiling

    def test_hitl_is_not_enabled(self):
        # The interrupt-propagation waiver depends on this: no human-in-the-loop.
        assert (_CONFIG.get("hitl") or {}).get("enabled", False) is False


class TestRuntimeConfigIsLive:
    def test_cfg_06_runtime_config_reads_the_shipped_file(self):
        assert _runtime_config() == _CONFIG

    def test_cfg_07_declared_settings_reach_the_inner_graph(self):
        # The declaration is the source of truth: what config/config.yaml says is
        # what the main slot forwards. A reader still pointing at a legacy
        # manifest block would return the module floor here instead.
        forwarded = TranslationWorkflowGraphNode(settings=declared_settings(_CONFIG))._parent_config()["configurable"][
            "settings"
        ]
        assert forwarded["cjk_threshold"] == _CONFIG["detection"]["cjk_threshold"]
        assert forwarded["max_caller_glossary_terms"] == (_CONFIG["translation"]["max_caller_glossary_terms"])

    def test_cfg_08_declared_value_wins_over_the_module_floor(self):
        # Declare something different from the shipped file and prove it travels.
        forwarded = TranslationWorkflowGraphNode(
            settings=declared_settings(
                {"detection": {"cjk_threshold": 0.75}, "translation": {"max_caller_glossary_terms": 3}}
            )
        )._parent_config()["configurable"]["settings"]
        assert forwarded["cjk_threshold"] == 0.75
        assert forwarded["max_caller_glossary_terms"] == 3

    def test_out_of_contract_declaration_falls_back_to_the_floor(self):
        # A non-finite or out-of-range declaration is not forwarded: NaN compares
        # False against every bound, so forwarding it would disable the detection
        # it configures rather than fail.
        for bad in (float("nan"), float("inf"), float("-inf"), -1, 999, "0.5", True, None):
            settings = declared_settings({"detection": {"cjk_threshold": bad}})
            assert settings["cjk_threshold"] == 0.05, f"unexpected forward for {bad!r}"
        for bad in (float("nan"), float("inf"), -1, 2.5, "4", True, None):
            settings = declared_settings({"translation": {"max_caller_glossary_terms": bad}})
            assert settings["max_caller_glossary_terms"] == 20, f"unexpected forward for {bad!r}"

    def test_the_outer_graph_seeds_the_settings_into_state(self):
        # ValidateInputNode reads the glossary cap from runtime_settings; the
        # outer graph must seed it from its constructor config.
        import json

        agent = ProfServicesTranslationAgent(config={"translation": {"max_caller_glossary_terms": 5}})
        seeded = json.loads(agent._extra_initial_state()["runtime_settings"])
        assert seeded["max_caller_glossary_terms"] == 5
