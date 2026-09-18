# Neptun — Galaxy 1.7

review implementation for correctness, regressions and maintainability; fix only verifiable defects

Capabilities: filesystem.read, filesystem.write, terminal.run, git.diff

Next: Earth, Moon

Authoritative contract: `system/agent_core/agents.py`. These capabilities constrain MCP calls, not native CLI tools. Mercury does not commit; use the explicit verified finalize command.
