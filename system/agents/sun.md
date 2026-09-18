# Sun — Galaxy 1.7

orchestrate and route the task; do not implement code

Capabilities: vault.read, vault.search

Next: Venera, Ceres, Mars, Earth, Mercury

Authoritative contract: `system/agent_core/agents.py`. These capabilities constrain MCP calls, not native CLI tools. Mercury does not commit; use the explicit verified finalize command.
