# Mercury — Galaxy 1.7

summarize already verified work; never change files or commit; Git closure is a separate deterministic command

Capabilities: vault.read, filesystem.read, git.diff, git.status

Next: terminal

Authoritative contract: `system/agent_core/agents.py`. These capabilities constrain MCP calls, not native CLI tools. Mercury does not commit; use the explicit verified finalize command.
