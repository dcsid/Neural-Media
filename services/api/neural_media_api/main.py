"""FastAPI app — SCAFFOLD STUB.

Owner: api-orchestrator worker. See docs/worker-briefs/api-orchestrator.md.

The integration lead has wired:
  - `create_app()` factory
  - `/api/v1/health` (so the frontend can detect "API offline")
  - CORS allowlist for the Next.js dev origin

Everything else (the contract endpoints in CONTRACTS.md §7) is for the
worker to implement. Do not change the route paths or response shapes
without coordinating — they are pinned by `shared/CONTRACTS.md` and
`shared/types.ts`.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def create_app() -> FastAPI:
    app = FastAPI(
        title="Neural Media API",
        version="0.0.1-scaffold",
        description=(
            "Local-only API. Serves predicted average BOLD fMRI responses "
            "(TRIBE v2) for a user's TikTok watch history. NOT a measurement "
            "of the user's own brain — see docs/scientific-framing.md."
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # TODO(api-orchestrator): mount the contract endpoints from
    # CONTRACTS.md §7. Keep them backed by SampleStore initially so the
    # vertical slice runs without GPU.

    return app


app = create_app()
