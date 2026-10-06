# config

Layered configuration (§35): `default.yaml` → `env/<APP_ENV>.yaml` → environment variables → DB runtime settings.

Phase 0 ships only the files needed to make `.env.example` dev-safe (`default.yaml` and `env/*.yaml`).
The schemas, `ce config validate` and every other YAML listed in §8 arrive in Phase 1.
