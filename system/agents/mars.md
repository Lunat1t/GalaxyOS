# Mars — Galaxy 1.7

analyze architecture, security, data boundaries and implementation risks

Capabilities: vault.read, filesystem.read, git.diff

Next: Earth

Authoritative contract: `system/agent_core/agents.py`. These capabilities constrain MCP calls, not native CLI tools. Mercury does not commit; use the explicit verified finalize command.
