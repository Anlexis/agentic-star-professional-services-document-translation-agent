"""AgentCore Platform v1.0"""

# AgentRegistry entry point.
#
# config/agent.yaml declares the entry point as the single dotted path
#
#   class: "src.graph.graph.ProfServicesTranslationAgent"
#
# so the registry resolves the class from the src.graph.graph module directly.
# The package __init__ re-exports it (with the `Graph` alias that
# src/api/server.py uses) so `from src.graph import ProfServicesTranslationAgent`
# works for callers that import the package rather than the module.

from .graph import Graph, ProfServicesTranslationAgent

__all__ = ["Graph", "ProfServicesTranslationAgent"]
