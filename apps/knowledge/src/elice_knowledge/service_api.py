"""Code-identity browser sessions and separate bearer-authenticated evaluation API."""

import secrets
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from elice_knowledge.access_codes import AccessCodes
from elice_knowledge.evaluation import SCORER, SCORER_KEY, dataset_snapshot, enqueue_evaluation
from elice_knowledge.execution import public_result
from elice_knowledge.human_review import HumanReview
from elice_knowledge.service_store import ServiceError, ServiceStore, token_hash

COOKIE = "knowledge_session"


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=256)


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    question: str = Field(min_length=1, max_length=4000)

    @field_validator("question")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Question must not be blank")
        return value.strip()


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str = Field(min_length=1, max_length=200)
    dataset_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    qa_profile_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    scorer_profile_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    record_ids: list[str] | None = Field(default=None, max_length=200)
    history_mode: str = Field(default="provided", pattern="^provided$")


class ProfileRequest(BaseModel):
    """An experiment profile: pin prompt versions/aliases without moving shared aliases."""

    model_config = ConfigDict(extra="forbid")
    index_key: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    top_k: int = Field(default=5, ge=1, le=20)
    prompts: dict[str, str] = Field(default_factory=dict, max_length=8)


class IssueCode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    expires_days: int = Field(default=30, ge=1, le=3660)
    requests_per_hour: int = Field(default=30, ge=1, le=1000)

    @field_validator("label")
    @classmethod
    def clean_label(cls, value):
        if not value.strip():
            raise ValueError("Label must not be blank")
        return value.strip()


class ExtendCode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: int = Field(ge=1, le=3660)


class ReviewImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class ReviewScore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Literal["pass", "fail", "not_applicable"]
    rationale: str = Field(min_length=1, max_length=4000)

    @field_validator("rationale")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Rationale must not be blank")
        return value.strip()


class ReviewSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    scores: dict[str, ReviewScore] = Field(min_length=1, max_length=4)
    draft_revision: int | None = Field(default=None, ge=0)


class ReviewDraftScore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Literal["", "pass", "fail", "not_applicable"]
    rationale: str = Field(default="", max_length=4000)


class ReviewDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    revision: int = Field(ge=0)
    scores: dict[str, ReviewDraftScore] = Field(min_length=1, max_length=4)


def admin_dependency(settings):
    def admin(request: Request):
        expected = settings.evaluation_api_token
        if not expected or not expected.get_secret_value():
            raise ServiceError("admin_not_configured", 503)
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(
            token_hash(supplied), token_hash("Bearer " + expected.get_secret_value())
        ):
            raise ServiceError("admin_authentication_required", 401)

    return admin


def mount_service(app, settings):
    store = ServiceStore(settings)
    router = APIRouter(prefix="/api")
    admin = admin_dependency(settings)

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"error": exc.code})

    def check_origin(request):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise ServiceError("invalid_origin", 403)

    def session(request: Request):
        row = store.session(request.cookies.get(COOKIE))
        if request.method not in {"GET", "HEAD"}:
            check_origin(request)
            csrf = request.headers.get("x-csrf-token", "")
            if not secrets.compare_digest(token_hash(csrf), row["csrf_hash"]):
                raise ServiceError("csrf_required", 403)
        return row

    session_dependency = Depends(session)

    def code_admin(owner=session_dependency):
        if owner["role"] != "admin":
            raise ServiceError("admin_required", 403)
        return owner

    code_admin_dependency = Depends(code_admin)
    codes = AccessCodes(store)
    reviews = HumanReview(store)

    @router.get("/review-candidates")
    def review_candidates(page_token: str | None = None, owner=session_dependency):
        return reviews.candidates(
            owner["principal_id"], page_token, include_origin=owner["role"] == "admin"
        )

    @router.post("/reviews", status_code=201)
    def start_review(body: ReviewImport, owner=session_dependency):
        return reviews.start(body.run_id, owner["principal_id"])

    @router.get("/admin/reviews/{run_id}/origin")
    def review_origin(run_id: str, owner=code_admin_dependency):
        return reviews.origin(run_id)

    @router.get("/reviews")
    def review_sources(owner=session_dependency):
        return {"items": reviews.sources(owner["principal_id"])}

    @router.get("/reviews/{run_id}/rounds/{number}/cases/{position}")
    def review_case(run_id: str, number: int, position: int, owner=session_dependency):
        return reviews.case(run_id, owner["principal_id"], number, position)

    @router.get("/reviews/{run_id}/rounds/{number}/progress")
    def review_progress(run_id: str, number: int, owner=session_dependency):
        return {"items": reviews.progress(run_id, owner["principal_id"], number)}

    @router.put("/reviews/{run_id}/rounds/{number}/cases/{position}/draft")
    def save_review_draft(
        run_id: str, number: int, position: int, body: ReviewDraft, owner=session_dependency
    ):
        return reviews.save_draft(
            run_id,
            owner["principal_id"],
            number,
            position,
            body.answer_digest,
            body.revision,
            {key: value.model_dump() for key, value in body.scores.items()},
        )

    @router.post("/reviews/{run_id}/rounds/{number}/cases/{position}")
    def save_review(
        run_id: str, number: int, position: int, body: ReviewSubmission, owner=session_dependency
    ):
        return reviews.save(
            run_id,
            owner["principal_id"],
            number,
            position,
            body.answer_digest,
            {key: value.model_dump() for key, value in body.scores.items()},
            body.draft_revision,
        )

    @router.get("/reviews/{run_id}/rounds/{number}/labels")
    def review_labels(run_id: str, number: int, owner=session_dependency):
        return {
            "labels": [
                label
                for row in reviews.labels(run_id, owner["principal_id"], number)
                for label in row["labels"]
            ]
        }

    @router.post("/reviews/{run_id}/rounds/{number}/sync")
    def sync_review(run_id: str, number: int, owner=session_dependency):
        return reviews.sync(run_id, owner["principal_id"], number)

    def identity(row):
        return {"id": row["principal_id"], "label": row["label"], "role": row["role"]}

    def turn_output(row):
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "run_id": (row["result"] or {}).get("run_id"),
            "question": row["question"],
            "status": row["status"],
            "stage": row["stage"],
            "result": public_result(row["result"]),
            "error": row["error"],
        }

    @router.get("/ui-config")
    def config():
        return {
            "name": "엘리스 테크블로그",
            "access": "code",
            "session_hours": settings.chat_session_hours,
        }

    @router.post("/auth/session")
    def login(body: Login, request: Request, response: Response):
        check_origin(request)
        token, csrf, row = store.login(
            body.code, request.client.host if request.client else "unknown"
        )
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            secure=settings.app_env == "prod",
            samesite="lax",
            max_age=settings.chat_session_hours * 3600,
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return {"csrf_token": csrf, "expires_at": row["expires_at"], "identity": identity(row)}

    @router.get("/auth/session")
    def get_session(request: Request, response: Response, owner=session_dependency):
        response.headers["Cache-Control"] = "no-store"
        return {
            "csrf_token": store.csrf(request.cookies[COOKIE]),
            "identity": identity(owner),
            "expires_at": owner["expires_at"],
        }

    @router.delete("/auth/session", status_code=204)
    def logout(response: Response, owner=session_dependency):
        store.logout(owner["id"])
        response.delete_cookie(COOKIE, path="/")

    @router.get("/conversations")
    def conversations(cursor: datetime | None = None, owner=session_dependency):
        return {"items": store.conversations(owner["principal_id"], cursor)}

    @router.post("/conversations", status_code=201)
    def create(owner=session_dependency):
        row = store.create_conversation(owner["principal_id"])
        return {"id": row["id"], "title": row["title"], "turns": []}

    @router.get("/conversations/{conversation_id}")
    def conversation(conversation_id: UUID, owner=session_dependency):
        row = store.conversation(owner["principal_id"], conversation_id)
        return {
            "id": row["id"],
            "title": row["title"],
            "turns": [turn_output(t) for t in row["turns"]],
        }

    @router.delete("/conversations/{conversation_id}", status_code=204)
    def delete(conversation_id: UUID, owner=session_dependency):
        store.conversation(owner["principal_id"], conversation_id, delete=True)

    @router.post("/conversations/{conversation_id}/turns", status_code=202)
    def submit(conversation_id: UUID, body: Turn, owner=session_dependency):
        turn_id = store.enqueue_turn(
            owner["principal_id"], conversation_id, body.request_id, body.question
        )
        return turn_output(store.turn(owner["principal_id"], turn_id))

    @router.get("/turns/{turn_id}")
    def turn(turn_id: UUID, owner=session_dependency):
        return turn_output(store.turn(owner["principal_id"], turn_id))

    @router.get("/admin/access-codes")
    def list_codes(status: str = "all", page: int = 1, owner=code_admin_dependency):
        if status not in {"all", "active", "expiring", "expired", "revoked"} or page < 1:
            raise ServiceError("invalid_code_filter", 422)
        return codes.list(owner["principal_id"], status, page)

    @router.post("/admin/access-codes", status_code=201)
    def create_code(body: IssueCode, owner=code_admin_dependency):
        return codes.issue(
            owner["principal_id"], body.label, body.expires_days, body.requests_per_hour
        )

    @router.post("/admin/access-codes/{code_id}/extend")
    def extend_code(code_id: UUID, body: ExtendCode, owner=code_admin_dependency):
        return codes.extend(owner["principal_id"], code_id, body.days)

    @router.delete("/admin/access-codes/{code_id}", status_code=204)
    def revoke_code(code_id: UUID, owner=code_admin_dependency):
        codes.revoke(owner["principal_id"], code_id)

    @router.get("/qa-profiles", dependencies=[Depends(admin)])
    def profiles():
        active = store.active_profile()
        with store.connection() as conn:
            return {
                "active": active,
                "items": conn.execute(
                    "SELECT * FROM qa_profiles ORDER BY created_at DESC"
                ).fetchall(),
            }

    @router.post("/qa-profiles", status_code=201, dependencies=[Depends(admin)])
    def create_profile(body: ProfileRequest):
        key = store.active_profile(body.index_key, body.top_k, body.prompts)
        prompts = store.profile(key)["prompts"]
        return {
            "key": key,
            "prompts": {
                name: {k: prompt.get(k) for k in ("source", "uri", "version", "digest")}
                for name, prompt in prompts.items()
            },
        }

    @router.get("/scorer-profiles", dependencies=[Depends(admin)])
    def scorers():
        return {
            "items": [{"key": SCORER_KEY, "config": SCORER}],
            "judges_enabled": settings.evaluation_judges_enabled,
        }

    @router.get("/evaluation-datasets/{dataset_id}", dependencies=[Depends(admin)])
    def dataset(dataset_id: str):
        try:
            snapshot = dataset_snapshot(settings, dataset_id)
            return {**snapshot, "record_count": len(snapshot["records"])}
        except ServiceError:
            raise
        except Exception:
            raise ServiceError("dataset_unavailable_or_invalid", 422) from None

    @router.post("/evaluations", status_code=202, dependencies=[Depends(admin)])
    def evaluate(body: EvaluationRequest):
        try:
            return {"id": enqueue_evaluation(store, body)}
        except ServiceError:
            raise
        except Exception:
            raise ServiceError("evaluation_enqueue_failed", 503) from None

    @router.get("/evaluations/{evaluation_id}", dependencies=[Depends(admin)])
    def evaluation(evaluation_id: UUID):
        with store.connection() as conn:
            row = conn.execute(
                """
                    SELECT j.id,j.status,j.stage,j.error,e.dataset_id,e.dataset_digest,
                    e.profile_key,e.mlflow_run_id,e.summary FROM qa_jobs j JOIN evaluation_runs
                    e ON e.id=j.id WHERE j.id=%s
                """,
                (evaluation_id,),
            ).fetchone()
        if not row:
            raise ServiceError("not_found", 404)
        return row

    @router.get("/evaluations/{evaluation_id}/cases", dependencies=[Depends(admin)])
    def cases(evaluation_id: UUID, limit: int = 50, offset: int = 0):
        evaluation(evaluation_id)
        with store.connection() as conn:
            rows = conn.execute(
                (
                    "SELECT * FROM evaluation_cases WHERE evaluation_id=%s ORDER BY record_id "
                    "LIMIT %s OFFSET %s"
                ),
                (evaluation_id, max(1, min(limit, 200)), max(0, offset)),
            ).fetchall()
        return {"items": rows}

    app.include_router(router)
    return admin
