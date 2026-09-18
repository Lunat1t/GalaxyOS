# Venera — Galaxy 1.7

turn the request into an explicit, testable specification

Capabilities: vault.read, vault.append, filesystem.read

Next: Mars, Ceres, Earth

Authoritative contract: `system/agent_core/agents.py`. These capabilities constrain MCP calls, not native CLI tools. Mercury does not commit; use the explicit verified finalize command.
