# Earth — Galaxy 3.1

implement the specification with minimal verified code changes

Capabilities: vault.read, filesystem.read, filesystem.write, terminal.run, git.diff

Routing: selected dynamically by the validated DAG.

Canonical runtime roles: `galaxy_core/engine/roles.py`. Agent authority is enforced by the core runtime and explicit tool scopes. Agents never commit.
