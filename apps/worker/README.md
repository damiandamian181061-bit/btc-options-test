# apps/worker

Long-running services deployed on the server with Docker Compose: the recorder (Phase 1), the
nightly backup and data checks (Phase 2) and the paper-trading service (Phases 2B and 8). They are
thin entry points: the logic lives in `src/optlab`.
