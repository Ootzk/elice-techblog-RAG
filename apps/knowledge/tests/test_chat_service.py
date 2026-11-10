"""Isolated PostgreSQL tests; generation and tracking never contact paid services."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from elice_knowledge.access_codes import AccessCodes
from elice_knowledge.evaluation import SCORER_KEY, enqueue_evaluation, score
from elice_knowledge.main import create_app
from elice_knowledge.prompts import PROMPT_NAMES, bundled
from elice_knowledge.qa import QAResponse
from elice_knowledge.service_api import EvaluationRequest
from elice_knowledge.service_store import (
    PROFILE_FIELDS,
    ServiceError,
    ServiceStore,
    code_identity,
    provider_identity,
)
from elice_knowledge.settings import Settings
from elice_knowledge.worker import process_one

PROFILE = "a" * 64


def registry_prompts():
    return {
        name: bundled(name)
        .model_copy(update={"source": "registry", "uri": f"prompts:/{name}/1", "version": 1})
        .model_dump()
        for name in PROMPT_NAMES
    }


@pytest.fixture
def service(store, monkeypatch):
    settings = Settings(
        database_url="postgresql://test@localhost/test",
        app_env="dev",
        auth_session_secret="test-session-secret-with-32-bytes",
        admin_access_code="test-admin-login-code",
        evaluation_api_token="test-admin",
        generation_model="test-model",
    )
    service = ServiceStore(settings)
    config = {name: getattr(settings, name) for name in PROFILE_FIELDS}
    config.update(
        code_identity=code_identity(),
        provider_identity=provider_identity(settings),
        index_key="b" * 64,
        top_k=5,
        prompts=registry_prompts(),
    )
    with service.connection() as conn:
        conn.execute("INSERT INTO qa_profiles(key,config) VALUES (%s,%s)", (PROFILE, Jsonb(config)))
    AccessCodes(service).bootstrap()
    _, _, owner = service.login("test-admin-login-code", "bootstrap")
    guest = AccessCodes(service).issue(owner["principal_id"], "test guest", 30, 2)
    service.test_guest_code = guest["code"]
    service.test_admin_id = owner["principal_id"]
    monkeypatch.setattr(ServiceStore, "active_profile", lambda self: PROFILE)
    return service


def browser(service, code=None):
    if code is None:
        code = AccessCodes(service).issue(service.test_admin_id, "separate guest", 30, 2)["code"]
    client = TestClient(create_app(service.settings))
    response = client.post("/api/auth/session", json={"code": code})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    client.headers["x-csrf-token"] = response.json()["csrf_token"]
    return client


def new_conversation(client):
    response = client.post("/api/conversations")
    assert response.status_code == 201
    return response.json()["id"]


def submit(client, conversation, request_id=None, question="엘리스는?"):
    return client.post(
        f"/api/conversations/{conversation}/turns",
        json={
            "request_id": str(request_id or uuid4()),
            "question": question,
        },
    )


def test_session_csrf_ownership_and_admin_boundary(service):
    alice, bob = browser(service), browser(service)
    conversation = new_conversation(alice)
    assert bob.get(f"/api/conversations/{conversation}").status_code == 404
    assert submit(bob, conversation).status_code == 404
    assert bob.delete(f"/api/conversations/{conversation}").status_code == 404
    csrf = alice.headers.pop("x-csrf-token")
    assert alice.post("/api/conversations").status_code == 403
    alice.headers["x-csrf-token"] = csrf
    # Reading in another tab must not invalidate the first tab's CSRF token.
    assert alice.get("/api/auth/session").json()["csrf_token"] == csrf
    assert (
        alice.post("/api/conversations", headers={"Origin": "https://other.example"}).status_code
        == 403
    )
    assert alice.get("/api/qa-profiles").status_code == 401
    assert alice.post("/qa", json={"question": "test", "index_key": "b" * 64}).status_code == 401
    assert (
        alice.get("/api/qa-profiles", headers={"Authorization": "Bearer test-admin"}).status_code
        == 200
    )
    assert alice.delete("/api/auth/session").status_code == 204
    assert alice.get("/api/conversations").status_code == 401


def test_duplicate_turn_busy_quota_and_worker_history(service, monkeypatch):
    client = browser(service)
    conversation = new_conversation(client)
    request = uuid4()
    first = submit(client, conversation, request)
    turn_id = first.json()["id"]
    assert submit(client, conversation, request).json()["id"] == turn_id
    assert submit(client, conversation, request, "다른 질문").status_code == 409
    assert submit(client, conversation).json()["error"] == "conversation_busy"
    seen = []

    def answer(settings, request, prompts):
        seen.append(request)
        return QAResponse(
            status="answered",
            answer="답변입니다.",
            index_key=request.index_key,
            generation_model="test-model",
        )

    monkeypatch.setattr("elice_knowledge.execution.run_qa", answer)
    assert process_one(service)
    assert not process_one(service)
    assert client.get(f"/api/turns/{turn_id}").json()["result"]["answer"] == "답변입니다."
    with service.connection() as conn:
        execution = conn.execute("SELECT result FROM qa_jobs WHERE id=%s", (turn_id,)).fetchone()[
            "result"
        ]
    timings = execution["timings"]
    assert timings["queue_wait_ms"] >= 0
    assert timings["worker_duration_ms"] >= execution["duration_ms"]
    assert timings["server_duration_ms"] == pytest.approx(
        timings["queue_wait_ms"] + timings["worker_duration_ms"]
    )
    assert len(seen) == 1
    assert submit(client, conversation, question="그것은?").status_code == 202
    process_one(service)
    assert [m.role for m in seen[1].history] == ["user", "assistant"]
    assert seen[1].history[1].content == "답변입니다."
    assert submit(client, conversation).status_code == 429
    assert client.delete(f"/api/conversations/{conversation}").status_code == 204
    assert client.get(f"/api/turns/{turn_id}").status_code == 404


def test_atomic_duplicate_and_quota_across_conversations(service):
    token, _, owner = service.login(service.test_guest_code, "local")
    conversation = service.create_conversation(owner["principal_id"])["id"]
    request = uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: service.enqueue_turn(owner["principal_id"], conversation, request, "Q"),
                range(2),
            )
        )
    assert len(set(results)) == 1
    service.finish(service.claim(), {"result": {"answer": "A"}})
    c1 = service.create_conversation(owner["principal_id"])["id"]
    c2 = service.create_conversation(owner["principal_id"])["id"]

    def enqueue(conv):
        try:
            return service.enqueue_turn(owner["principal_id"], conv, uuid4(), "Q2")
        except ServiceError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enqueue, [c1, c2]))
    assert results.count("chat_rate_limited") == 1
    assert service.session(token)["id"] == owner["id"]


def test_lease_expiry_never_replays_generation(service):
    client = browser(service)
    turn_id = submit(client, new_conversation(client)).json()["id"]
    job = service.claim()
    with service.connection() as conn:
        conn.execute(
            "UPDATE qa_jobs SET lease_expires=now()-interval '1 second' WHERE id=%s", (turn_id,)
        )
    assert service.claim() is None
    assert client.get(f"/api/turns/{turn_id}").json()["status"] == "interrupted"
    service.finish(job, {"result": {"answer": "late"}})
    assert client.get(f"/api/turns/{turn_id}").json()["result"] is None


def test_expired_session_preserves_code_history_and_queued_work(service):
    client = browser(service, service.test_guest_code)
    conversation = new_conversation(client)
    submit(client, conversation)
    with service.connection() as conn:
        conn.execute("UPDATE chat_sessions SET expires_at=now()-interval '2 days'")
    service.cleanup()
    # Code-owned conversations do not depend on the lifetime of the cookie.
    again = browser(service, service.test_guest_code)
    assert again.get(f"/api/conversations/{conversation}").status_code == 200
    assert service.claim() is not None
    with service.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM chat_sessions").fetchone()["n"] == 1


def test_failed_worker_is_terminal_and_sanitizes_provider_errors(service, monkeypatch):
    client = browser(service)
    turn_id = submit(client, new_conversation(client)).json()["id"]

    def failed(*args):
        raise RuntimeError("secret provider url and API key")

    monkeypatch.setattr("elice_knowledge.execution.run_qa", failed)
    process_one(service)
    result = client.get(f"/api/turns/{turn_id}").json()
    assert result["status"] == "failed" and result["error"] == "RuntimeError"
    assert "secret" not in str(result)
    assert not process_one(service)


def test_login_rate_limit_and_secure_cookie(service):
    service.settings.chat_login_attempts_per_minute = 1
    client = TestClient(create_app(service.settings))
    assert client.post("/api/auth/session", json={"code": "wrong"}).status_code == 401
    assert (
        client.post("/api/auth/session", json={"code": service.test_guest_code}).status_code == 429
    )
    service.settings.app_env = "prod"
    service.settings.chat_login_attempts_per_minute = 10
    response = client.post("/api/auth/session", json={"code": service.test_guest_code})
    assert "Secure" in response.headers["set-cookie"]


def test_evaluation_uses_only_inputs_and_persists_scores(service, monkeypatch):
    records = [
        {
            "id": "record-1",
            "inputs": {"question": "Q", "history": []},
            "expectations": {
                "answerable": True,
                "answer": "GOLD MUST NEVER ENTER PROMPT",
                "evidence": [{"url": "https://elice.io/ko/test", "quote": "검증 근거"}],
            },
        }
    ]
    monkeypatch.setattr(
        "elice_knowledge.evaluation.dataset_snapshot",
        lambda *a: {"digest": "d" * 64, "records": records},
    )
    calls = []

    def answer(settings, request, prompts):
        calls.append(request.model_dump())
        return QAResponse(
            status="answered",
            answer="근거 [1]",
            index_key=request.index_key,
            generation_model="test-model",
            retrieved_sources=[{"url": "https://elice.io/ko/test", "text": "검증 근거 내용"}],
        )

    monkeypatch.setattr("elice_knowledge.execution.run_qa", answer)

    links, params = [], {}

    class Tracking:
        def get_experiment_by_name(self, *a):
            return SimpleNamespace(experiment_id="experiment")

        def create_run(self, *a, **kw):
            return SimpleNamespace(info=SimpleNamespace(run_id="mlflow-run"))

        def link_prompt_version_to_run(self, run_id, uri):
            links.append((run_id, uri))

        def log_param(self, run_id, key, value):
            params[key] = value

        def __getattr__(self, name):
            return lambda *a, **kw: None

    monkeypatch.setattr("elice_knowledge.evaluation.tracking_client", lambda *a: Tracking())
    request = EvaluationRequest(
        dataset_id="dataset",
        dataset_digest="d" * 64,
        qa_profile_key=PROFILE,
        scorer_profile_key=SCORER_KEY,
    )
    job_id = enqueue_evaluation(service, request)
    records[0]["inputs"]["question"] = "changed after snapshot"
    assert process_one(service)
    assert calls[0]["question"] == "Q" and "GOLD" not in str(calls)
    assert sorted(uri for _, uri in links) == sorted(f"prompts:/{n}/1" for n in PROMPT_NAMES)
    assert params[f"prompt.{PROMPT_NAMES[0]}.digest"] == bundled(PROMPT_NAMES[0]).digest
    client = TestClient(
        create_app(service.settings), headers={"Authorization": "Bearer test-admin"}
    )
    result = client.get(f"/api/evaluations/{job_id}").json()
    assert result["status"] == "completed"
    assert "evidence_recall" not in result["summary"]
    assert (
        client.get(f"/api/evaluations/{job_id}/cases").json()["items"][0]["scores"][
            "answerability_correct"
        ]
        == 1
    )


def test_answerability_is_a_deterministic_status_check_only():
    assert score({"status": "answered"}, {"answerable": True}) == {"answerability_correct": 1}
    assert score({"status": "insufficient_evidence"}, {"answerable": True}) == {
        "answerability_correct": 0
    }
    assert score({"status": "insufficient_evidence"}, {"answerable": False}) == {
        "answerability_correct": 1
    }


def test_global_worker_concurrency_and_profile_drift(service):
    client = browser(service)
    submit(client, new_conversation(client))
    submit(client, new_conversation(client))
    first = service.claim()
    assert service.claim() is None
    service.finish(first, error="test_failure")
    assert service.claim() is not None
    service.settings.generation_base_url = SecretStr("https://changed.example/v1")
    with pytest.raises(ServiceError, match="qa_profile_provider_changed"):
        service.profile(PROFILE)


def test_mlflow_failure_marks_evaluation_failed_without_generation(service, monkeypatch):
    monkeypatch.setattr(
        "elice_knowledge.evaluation.dataset_snapshot",
        lambda *a: {
            "digest": "d" * 64,
            "records": [
                {
                    "id": "r1",
                    "inputs": {"question": "Q", "history": []},
                    "expectations": {"answerable": False},
                }
            ],
        },
    )

    def no_tracking(*args):
        raise RuntimeError("private tracking details")

    monkeypatch.setattr("elice_knowledge.evaluation.tracking_client", no_tracking)
    monkeypatch.setattr(
        "elice_knowledge.execution.run_qa", lambda *a: pytest.fail("No paid call before tracking")
    )
    job_id = enqueue_evaluation(
        service,
        EvaluationRequest(
            dataset_id="dataset",
            dataset_digest="d" * 64,
            qa_profile_key=PROFILE,
            scorer_profile_key=SCORER_KEY,
        ),
    )
    process_one(service)
    with service.connection() as conn:
        row = conn.execute("SELECT status,error FROM qa_jobs WHERE id=%s", (job_id,)).fetchone()
    assert row == {"status": "failed", "error": "RuntimeError"}


def test_same_code_recovers_history_after_logout_and_on_another_device(service):
    first = browser(service, service.test_guest_code)
    conversation = new_conversation(first)
    second = browser(service, service.test_guest_code)
    assert second.get(f"/api/conversations/{conversation}").status_code == 200
    assert first.delete("/api/auth/session").status_code == 204
    assert second.get(f"/api/conversations/{conversation}").status_code == 200
    again = browser(service, service.test_guest_code)
    assert again.get("/api/conversations").json()["items"][0]["id"] == conversation
    assert browser(service).get(f"/api/conversations/{conversation}").status_code == 404


def test_admin_issues_guest_codes_without_exposing_hash_or_plaintext_in_lists(service):
    admin = browser(service, "test-admin-login-code")
    created = admin.post(
        "/api/admin/access-codes",
        json={
            "label": "리뷰어",
            "expires_days": 7,
            "requests_per_hour": 10,
        },
    )
    assert created.status_code == 201
    code = created.json()
    assert len(code["code"]) >= 24
    guest = browser(service, code["code"])
    session = guest.get("/api/auth/session").json()
    assert session["identity"] == {"id": code["id"], "label": "리뷰어", "role": "guest"}
    listing = admin.get("/api/admin/access-codes").json()
    assert code["code"] not in str(listing) and "code_hash" not in str(listing)
    with service.connection() as conn:
        rows = conn.execute(
            "SELECT details FROM access_code_events WHERE access_code_id=%s", (code["id"],)
        ).fetchall()
        assert code["code"] not in str(rows)
    assert (
        admin.post("/api/admin/access-codes", json={"label": " ", "expires_days": 1}).status_code
        == 422
    )
    assert (
        admin.post("/api/admin/access-codes", json={"label": "a", "kind": "admin"}).status_code
        == 422
    )


def test_guest_cannot_manage_codes_and_admin_mutations_require_csrf(service):
    guest, admin = browser(service), browser(service, "test-admin-login-code")
    assert guest.get("/api/admin/access-codes").status_code == 403
    assert guest.post("/api/admin/access-codes", json={"label": "escalation"}).status_code == 403
    assert (
        guest.post(
            f"/api/admin/access-codes/{service.test_admin_id}/extend", json={"days": 1}
        ).status_code
        == 403
    )
    assert guest.delete(f"/api/admin/access-codes/{service.test_admin_id}").status_code == 403
    admin.headers.pop("x-csrf-token")
    assert admin.post("/api/admin/access-codes", json={"label": "missing csrf"}).status_code == 403
    anonymous = TestClient(
        create_app(service.settings), headers={"Authorization": "Bearer test-admin"}
    )
    assert anonymous.get("/api/admin/access-codes").status_code == 401


def test_extension_preserves_code_history_and_revocation_is_final(service):
    admin = browser(service, "test-admin-login-code")
    created = admin.post(
        "/api/admin/access-codes", json={"label": "review", "expires_days": 1}
    ).json()
    guest = browser(service, created["code"])
    conversation = new_conversation(guest)
    with service.connection() as conn:
        conn.execute(
            "UPDATE access_codes SET expires_at=now()-interval '1 day' WHERE id=%s",
            (created["id"],),
        )
    assert guest.get("/api/auth/session").status_code == 401
    assert admin.get("/api/admin/access-codes?status=expired").json()["total"] == 1
    response = admin.post(f"/api/admin/access-codes/{created['id']}/extend", json={"days": 7})
    assert response.status_code == 200
    assert guest.get(f"/api/conversations/{conversation}").status_code == 200
    again = browser(service, created["code"])
    assert again.get(f"/api/conversations/{conversation}").status_code == 200
    with service.connection() as conn:
        before = conn.execute(
            "SELECT expires_at FROM access_codes WHERE id=%s", (created["id"],)
        ).fetchone()["expires_at"]
    admin.post(f"/api/admin/access-codes/{created['id']}/extend", json={"days": 3})
    with service.connection() as conn:
        after = conn.execute(
            "SELECT expires_at FROM access_codes WHERE id=%s", (created["id"],)
        ).fetchone()["expires_at"]
    assert (after - before).days == 3
    assert admin.delete(f"/api/admin/access-codes/{created['id']}").status_code == 204
    assert guest.get("/api/auth/session").status_code == 401
    assert again.get("/api/auth/session").status_code == 401
    assert (
        admin.post(f"/api/admin/access-codes/{created['id']}/extend", json={"days": 1}).status_code
        == 409
    )
    assert (
        TestClient(create_app(service.settings))
        .post("/api/auth/session", json={"code": created["code"]})
        .status_code
        == 401
    )
    assert admin.delete(f"/api/admin/access-codes/{service.test_admin_id}").status_code == 409


def test_revoked_code_queued_work_is_interrupted_without_generation(service):
    admin = browser(service, "test-admin-login-code")
    guest = browser(service, service.test_guest_code)
    turn_id = submit(guest, new_conversation(guest)).json()["id"]
    code_id = guest.get("/api/auth/session").json()["identity"]["id"]
    admin.delete(f"/api/admin/access-codes/{code_id}")
    assert service.claim() is None
    with service.connection() as conn:
        row = conn.execute("SELECT status,error FROM qa_jobs WHERE id=%s", (turn_id,)).fetchone()
    assert row == {"status": "interrupted", "error": "access_code_inactive"}


def test_admin_html_is_protected_and_cannot_be_accessed_with_guest_cookie(service, tmp_path):
    (tmp_path / "index.html").write_text("<html>admin app shell</html>")
    service.settings.web_dist_dir = tmp_path
    anonymous = TestClient(create_app(service.settings))
    assert anonymous.get("/admin/authentication", follow_redirects=False).status_code == 303
    assert browser(service).get("/admin/authentication").status_code == 403
    admin = browser(service, "test-admin-login-code")
    response = admin.get("/admin/authentication")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert admin.get("/admin/unknown").status_code == 404


def test_session_secret_is_independent_and_bootstrap_is_idempotent(service):
    client = browser(service, service.test_guest_code)
    conversation = new_conversation(client)
    assert client.get("/api/auth/session").status_code == 200
    AccessCodes(service).bootstrap()
    with service.connection() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM access_codes WHERE kind='admin'").fetchone()[
                "n"
            ]
            == 1
        )
    service.settings.auth_session_secret = SecretStr("new-server-secret-for-cookie-rotation")
    assert client.get("/api/auth/session").status_code == 401
    again = browser(service, service.test_guest_code)
    assert again.get(f"/api/conversations/{conversation}").status_code == 200
    with pytest.raises(ServiceError, match="last_admin_cannot_be_revoked"):
        AccessCodes(service).revoke_admin(service.test_admin_id)


def test_code_quota_cannot_be_reset_by_logging_in_again(service):
    first = browser(service, service.test_guest_code)
    submit(first, new_conversation(first))
    first.delete("/api/auth/session")
    second = browser(service, service.test_guest_code)
    assert submit(second, new_conversation(second)).status_code == 202
    third = browser(service, service.test_guest_code)
    assert submit(third, new_conversation(third)).status_code == 429
    admin = browser(service, "test-admin-login-code")
    assert all(submit(admin, new_conversation(admin)).status_code == 202 for _ in range(3))


def test_deleted_code_conversation_is_physically_cleaned_after_grace(service, monkeypatch):
    client = browser(service, service.test_guest_code)
    conversation = new_conversation(client)
    turn_id = submit(client, conversation).json()["id"]
    monkeypatch.setattr(
        "elice_knowledge.execution.run_qa",
        lambda settings, request, prompts: QAResponse(
            status="answered",
            answer="test answer",
            index_key=request.index_key,
            generation_model="test-model",
        ),
    )
    process_one(service)
    assert client.delete(f"/api/conversations/{conversation}").status_code == 204
    service.cleanup()
    with service.connection() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM qa_jobs WHERE id=%s", (turn_id,)).fetchone()[
                "n"
            ]
            == 1
        )
        conn.execute(
            "UPDATE chat_conversations SET deleted_at=now()-interval '2 days' WHERE id=%s",
            (conversation,),
        )
    service.cleanup()
    with service.connection() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM qa_jobs WHERE id=%s", (turn_id,)).fetchone()[
                "n"
            ]
            == 0
        )
        assert conn.execute("SELECT count(*) AS n FROM qa_runs").fetchone()["n"] == 0
    assert client.get("/api/auth/session").status_code == 200


def test_revoked_admin_does_not_return_when_bootstrap_is_repeated(service):
    codes = AccessCodes(service)
    original = service.settings.admin_access_code
    service.settings.admin_access_code = SecretStr("additional-admin-login-code")
    codes.bootstrap()
    codes.revoke_admin(service.test_admin_id)
    service.settings.admin_access_code = original
    codes.bootstrap()
    with pytest.raises(ServiceError, match="invalid_access_code"):
        service.login(original.get_secret_value(), "test-admin-revoked")
    _, _, owner = service.login("additional-admin-login-code", "test-admin-active")
    assert owner["role"] == "admin"


def test_evaluation_rejects_profiles_resolved_from_bundled_prompts(service):
    config = {"prompts": {n: bundled(n, "registry_unavailable").model_dump() for n in PROMPT_NAMES}}
    with service.connection() as conn:
        base = conn.execute("SELECT config FROM qa_profiles WHERE key=%s", (PROFILE,)).fetchone()
        conn.execute(
            "INSERT INTO qa_profiles(key,config) VALUES (%s,%s)",
            ("c" * 64, Jsonb({**base["config"], **config})),
        )
    request = EvaluationRequest(
        dataset_id="dataset",
        dataset_digest="d" * 64,
        qa_profile_key="c" * 64,
        scorer_profile_key=SCORER_KEY,
    )
    with pytest.raises(ServiceError) as error:
        enqueue_evaluation(service, request)
    assert error.value.code == "evaluation_requires_registry_prompts"
    assert error.value.status == 409


def test_experiment_profile_api_pins_prompt_selectors(service, monkeypatch):
    seen = []

    def active(self, index_key=None, top_k=5, prompt_selectors=None):
        seen.append((index_key, top_k, prompt_selectors))
        return PROFILE

    monkeypatch.setattr(ServiceStore, "active_profile", active)
    client = TestClient(
        create_app(service.settings), headers={"Authorization": "Bearer test-admin"}
    )
    body = {"top_k": 3, "prompts": {PROMPT_NAMES[0]: "7"}}
    response = client.post("/api/qa-profiles", json=body)
    assert response.status_code == 201 and response.json()["key"] == PROFILE
    assert seen == [(None, 3, {PROMPT_NAMES[0]: "7"})]
    summary = response.json()["prompts"][PROMPT_NAMES[0]]
    assert summary["source"] == "registry" and "template" not in summary
    assert client.post("/api/qa-profiles", json={"unexpected": 1}).status_code == 422
    assert (
        TestClient(create_app(service.settings)).post("/api/qa-profiles", json={}).status_code
        == 401
    )


def test_evaluation_pins_judge_versions_and_persists_verdicts(service, monkeypatch):
    service.settings.evaluation_judges_enabled = True
    record = {
        "id": "r1",
        "inputs": {"question": "Q", "history": []},
        "expectations": {"answerable": False},
    }
    monkeypatch.setattr(
        "elice_knowledge.evaluation.dataset_snapshot",
        lambda *a: {"kind": "gold", "digest": "d" * 64, "records": [record]},
    )
    specs = [{"name": "qa-gold-chatgpt", "version": 1}]
    monkeypatch.setattr("elice_knowledge.evaluation.frozen_judges", lambda *a: specs)

    class Tracking:
        def get_experiment_by_name(self, *a):
            return SimpleNamespace(experiment_id="exp")

        def create_run(self, *a, **kw):
            return SimpleNamespace(info=SimpleNamespace(run_id="mlflow-judged"))

        def __getattr__(self, name):
            return lambda *a, **kw: None

    monkeypatch.setattr("elice_knowledge.evaluation.tracking_client", lambda *a: Tracking())
    monkeypatch.setattr(
        "elice_knowledge.execution.run_qa",
        lambda *a: QAResponse(
            status="insufficient_evidence",
            answer="근거 부족",
            index_key="b" * 64,
            reason="insufficient",
        ),
    )
    seen = []

    def judge(store, bundle, frozen):
        seen.append(frozen)
        assert bundle["cases"][0]["error"] is None
        return [{"run_id": "model-child", "state": "FINISHED", "rater_id": "model:chatgpt"}]

    monkeypatch.setattr("elice_knowledge.evaluation.evaluate_models", judge)
    job_id = enqueue_evaluation(
        service,
        EvaluationRequest(
            dataset_id="dataset",
            dataset_digest="d" * 64,
            qa_profile_key=PROFILE,
            scorer_profile_key=SCORER_KEY,
        ),
    )
    specs[0]["version"] = 2
    process_one(service)
    assert seen[0][0]["version"] == 1
    with service.connection() as conn:
        case = conn.execute(
            "SELECT scores FROM evaluation_cases WHERE evaluation_id=%s", (job_id,)
        ).fetchone()
        job = conn.execute("SELECT status FROM qa_jobs WHERE id=%s", (job_id,)).fetchone()
    assert job["status"] == "completed"
    assert "qa-gold-chatgpt" not in case["scores"]
    with service.connection() as conn:
        summary = conn.execute(
            "SELECT summary FROM evaluation_runs WHERE id=%s", (job_id,)
        ).fetchone()["summary"]
    assert summary["grading_runs"][0]["run_id"] == "model-child"


@pytest.mark.parametrize("changed_field", ["code_identity", "provider_identity"])
def test_existing_chat_refreshes_stale_profile_after_deployment(
    service, monkeypatch, changed_field
):
    client = browser(service)
    conversation = new_conversation(client)
    fresh_key = "c" * 64
    with service.connection() as conn:
        config = conn.execute("SELECT config FROM qa_profiles WHERE key=%s", (PROFILE,)).fetchone()[
            "config"
        ]
        conn.execute(
            "INSERT INTO qa_profiles(key,config) VALUES (%s,%s)", (fresh_key, Jsonb(config))
        )
        stale = {**config, changed_field: "old-deployment"}
        conn.execute("UPDATE qa_profiles SET config=%s WHERE key=%s", (Jsonb(stale), PROFILE))
    monkeypatch.setattr(ServiceStore, "active_profile", lambda self: fresh_key)
    monkeypatch.setattr(
        "elice_knowledge.execution.run_qa",
        lambda settings, request, prompts: QAResponse(
            status="answered",
            answer="배포 후 답변",
            index_key=request.index_key,
            generation_model="test-model",
        ),
    )
    turn = submit(client, conversation)
    assert turn.status_code == 202
    assert process_one(service)
    assert (
        client.get("/api/turns/" + turn.json()["id"]).json()["result"]["answer"] == "배포 후 답변"
    )
    with service.connection() as conn:
        conv = conn.execute(
            "SELECT profile_key FROM chat_conversations WHERE id=%s", (conversation,)
        ).fetchone()
    assert conv["profile_key"] == fresh_key
    # Frozen evaluation profiles must continue to reject changed execution conditions.
    with pytest.raises(ServiceError, match="qa_profile_.*_changed"):
        service.profile(PROFILE)
