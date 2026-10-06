# Venera — Galaxy 3.1

turn the request into an explicit, testable specification

Capabilities: vault.read, vault.append, filesystem.read

Routing: selected dynamically by the validated DAG.

Canonical runtime roles: `galaxy_core/engine/roles.py`. Agent authority is enforced by the core runtime and explicit tool scopes. Agents never commit.
