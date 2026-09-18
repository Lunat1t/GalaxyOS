# Earth — Galaxy 1.7

implement the specification with minimal verified code changes

Capabilities: vault.read, filesystem.read, filesystem.write, terminal.run, git.diff

Next: Neptun, Moon

Authoritative contract: `system/agent_core/agents.py`. These capabilities constrain MCP calls, not native CLI tools. Mercury does not commit; use the explicit verified finalize command.
