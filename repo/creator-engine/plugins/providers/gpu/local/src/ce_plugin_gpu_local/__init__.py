"""Local provider (§25): statically started workers (compose `worker-cpu` with the real CPU engines,
a self-managed GPU host dialing out). It provisions nothing; it exists so pools of real workers
pass the router's pool filter without the mock provider."""
