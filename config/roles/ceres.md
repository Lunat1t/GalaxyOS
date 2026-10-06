# Ceres — Galaxy 3.1

research local project context and gather evidence; do not invent missing facts

Capabilities: vault.read, vault.search, filesystem.read

Routing: selected dynamically by the validated DAG.

Canonical runtime roles: `galaxy_core/engine/roles.py`. Agent authority is enforced by the core runtime and explicit tool scopes. Agents never commit.
