"""Mock adapters for every capability (§24, §37).

Each subpackage is one plugin with its own `plugin.yaml`. Mock model adapters run in the
`cpu_model` family on `worker-cpu` through the scheduler; mock analyzers, QC metrics, effects and
provenance run in the `cpu_inproc` family inside the orchestrator and render worker. All of them
produce real media (FFmpeg, Pillow, numpy), deterministic given the seed, and are registered only
when `MOCK_GPU=true`. They never claim to be models: their outputs carry burned labels or
`mock: true` metadata.
"""
