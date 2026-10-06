# plugins

Each plugin is a Python package with a `plugin.yaml` manifest (§24), an adapter implementing the
`ce_contracts` interfaces (§23) and, when it is behavior-capable, a `BehaviorTranslator`.
Plugins are discovered through the `creator_engine.plugins` entry-point group.

Core packages never import plugin packages (invariant I14).
