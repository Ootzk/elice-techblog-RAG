from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from elice_knowledge.database import check_database
from elice_knowledge.embedding import OllamaEmbedder
from elice_knowledge.generation import GenerationError, GenerationNotConfigured, GenerationTimeout
from elice_knowledge.qa import QARequest, QAResponse, QARetrievalError, run_qa
from elice_knowledge.settings import Settings
from elice_knowledge.tracking import check_tracking


class DependencyStatus(BaseModel):
    status: str
    detail: str | None = None


class Readiness(BaseModel):
    status: str
    dependencies: dict[str, DependencyStatus]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Elice Knowledge", version="0.1.0")

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", response_model=Readiness, responses={503: {"model": Readiness}})
    def ready():
        def embedding_check():
            with httpx.Client() as http:
                OllamaEmbedder(settings, http).profile()

        checks = {"database": lambda: check_database(settings), "embedding": embedding_check}
        if settings.mlflow_enabled:
            checks["mlflow"] = lambda: check_tracking(settings)

        def probe(name, check):
            try:
                check()
                return name, DependencyStatus(status="ok")
            except Exception as exc:
                # Upstream exception strings can contain credentials, SQL and private endpoints.
                return name, DependencyStatus(
                    status="error",
                    detail=f"{type(exc).__name__}: check {name} configuration and connectivity",
                )

        with ThreadPoolExecutor(max_workers=len(checks)) as executor:
            futures = [executor.submit(probe, name, check) for name, check in checks.items()]
            dependencies = dict(f.result() for f in futures)
        if not settings.mlflow_enabled:
            dependencies["mlflow"] = DependencyStatus(status="disabled")
        ok = all(item.status in {"ok", "disabled"} for item in dependencies.values())
        # Configuration-only: readiness must never trigger a paid generation call.
        dependencies["generation"] = DependencyStatus(
            status="configured" if settings.generation_configured else "not_configured",
            detail="Configuration only; generation connectivity has not been probed",
        )
        result = Readiness(status="ready" if ok else "not_ready", dependencies=dependencies)
        return JSONResponse(status_code=200 if ok else 503, content=result.model_dump())

    @app.post(
        "/qa",
        response_model=QAResponse,
        responses={
            502: {"description": "Generation failed or returned invalid output"},
            503: {"description": "Generation is not configured or retrieval is unavailable"},
            504: {"description": "Generation request timed out"},
        },
    )
    def qa(request: QARequest):
        try:
            return run_qa(settings, request)
        except GenerationNotConfigured as exc:
            return JSONResponse(status_code=503, content={"error": exc.code})
        except GenerationTimeout as exc:
            return JSONResponse(status_code=504, content={"error": exc.code})
        except GenerationError as exc:
            return JSONResponse(status_code=502, content={"error": exc.code})
        except QARetrievalError:
            return JSONResponse(status_code=503, content={"error": "retrieval_unavailable"})

    return app
