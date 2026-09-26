# Mercury — Galaxy 3.1

summarize already verified work; never change files or commit; Git closure is a separate deterministic command

Capabilities: vault.read, filesystem.read, git.diff, git.status

Routing: selected dynamically by the validated DAG.

Canonical runtime roles: `galaxy_core/engine/roles.py`. Agent authority is enforced by the core runtime and explicit tool scopes. Agents never commit.
