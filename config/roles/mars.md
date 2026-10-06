# Mars — Galaxy 3.1

analyze architecture, security, data boundaries and implementation risks

Capabilities: vault.read, filesystem.read, git.diff

Routing: selected dynamically by the validated DAG.

Canonical runtime roles: `galaxy_core/engine/roles.py`. Agent authority is enforced by the core runtime and explicit tool scopes. Agents never commit.
