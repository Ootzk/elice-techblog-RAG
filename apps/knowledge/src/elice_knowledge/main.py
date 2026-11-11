from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from importlib.metadata import version

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from elice_knowledge.agent import run_qa
from elice_knowledge.database import check_database
from elice_knowledge.embedding import create_embedder
from elice_knowledge.generation import GenerationError, GenerationNotConfigured, GenerationTimeout
from elice_knowledge.mcp_server import MCP_PATH, create_mcp_app
from elice_knowledge.prompts import PromptError, resolve
from elice_knowledge.qa import QARequest, QAResponse, QARetrievalError
from elice_knowledge.service_api import COOKIE, mount_service
from elice_knowledge.service_store import ServiceError, ServiceStore
from elice_knowledge.settings import Settings
from elice_knowledge.sparse import start_warm_up
from elice_knowledge.tracking import check_tracking


class DependencyStatus(BaseModel):
    status: str
    detail: str | None = None


class Readiness(BaseModel):
    status: str
    dependencies: dict[str, DependencyStatus]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    mcp_app, mcp_sessions = create_mcp_app(settings)

    @asynccontextmanager
    async def lifespan(_app):
        # /qa and /mcp keyword searches run in this process.
        start_warm_up(settings)
        async with mcp_sessions.run():
            yield

    app = FastAPI(title="Elice Knowledge", version=version("elice-knowledge"), lifespan=lifespan)
    # An exact ASGI route (not a Mount) so /mcp answers without a slash redirect;
    # registered before the web catch-all route.
    app.add_route(MCP_PATH, mcp_app)

    admin = mount_service(app, settings)

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith(("/api/", "/qa", MCP_PATH)):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", response_model=Readiness, responses={503: {"model": Readiness}})
    def ready():
        def embedding_check():
            with httpx.Client() as http:
                create_embedder(settings, http).profile()

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
        if (
            settings.embedding_backend == "elice-serverless"
            and dependencies["embedding"].status == "ok"
        ):
            dependencies["embedding"] = DependencyStatus(
                status="configured",
                detail="Configuration only; embedding connectivity has not been probed",
            )
        if not settings.mlflow_enabled:
            dependencies["mlflow"] = DependencyStatus(status="disabled")
        ok = all(item.status in {"ok", "configured", "disabled"} for item in dependencies.values())
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
        dependencies=[Depends(admin)],
        responses={
            502: {"description": "Generation failed or returned invalid output"},
            503: {"description": "Generation is not configured or retrieval is unavailable"},
            504: {"description": "Generation request timed out"},
        },
    )
    def qa(request: QARequest):
        try:
            # Immediate admin path: no stored profile, so resolve the configured alias now.
            request_settings = settings.model_copy(
                update={
                    "generation_options": request.generation_options,
                    "embedding_options": request.embedding_options,
                }
            )
            return run_qa(request_settings, request, resolve(request_settings))
        except PromptError as exc:
            return JSONResponse(status_code=422, content={"error": exc.code})
        except GenerationNotConfigured as exc:
            return JSONResponse(status_code=503, content={"error": exc.code})
        except GenerationTimeout as exc:
            return JSONResponse(status_code=504, content={"error": exc.code})
        except GenerationError as exc:
            return JSONResponse(status_code=502, content={"error": exc.code})
        except QARetrievalError:
            return JSONResponse(status_code=503, content={"error": "retrieval_unavailable"})

    if settings.web_dist_dir.is_dir():
        assets = settings.web_dist_dir / "_app"
        if assets.is_dir():
            app.mount("/_app", StaticFiles(directory=assets), name="web-assets")

        @app.get("/{path:path}", include_in_schema=False)
        def web(path: str, request: Request):
            if path == "admin" or path.startswith("admin/"):
                try:
                    owner = ServiceStore(settings).session(request.cookies.get(COOKIE))
                except ServiceError:
                    return RedirectResponse("/", status_code=303)
                if owner["role"] != "admin":
                    raise HTTPException(status_code=403)
                if path not in {"admin", "admin/", "admin/authentication", "admin/authentication/"}:
                    raise HTTPException(status_code=404)
                response = FileResponse(settings.web_dist_dir / "index.html")
                response.headers["Cache-Control"] = "no-store"
                return response
            if path.startswith(("api/", "health/", "mcp/")) or path in {"qa", "mcp"}:
                raise HTTPException(status_code=404)
            candidate = (settings.web_dist_dir / path).resolve()
            if candidate.is_relative_to(settings.web_dist_dir.resolve()) and candidate.is_file():
                return FileResponse(candidate)
            if path in {"review", "review/"}:
                try:
                    ServiceStore(settings).session(request.cookies.get(COOKIE))
                except ServiceError:
                    return RedirectResponse("/", status_code=303)
                response = FileResponse(settings.web_dist_dir / "index.html")
                response.headers["Cache-Control"] = "no-store"
                return response
            if not path or path.startswith("c/"):
                return FileResponse(settings.web_dist_dir / "index.html")
            raise HTTPException(status_code=404)

    return app
