"""Real PostgreSQL/auth boundaries, frozen answers, and a mocked MLflow artifact store."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_chat_service import browser
from test_chat_service import service as service  # noqa: F401

from elice_knowledge.evaluation_bundle import SCHEMA, create_bundle
from elice_knowledge.main import create_app

RUN = "a1" * 16


@pytest.fixture
def review(service, monkeypatch):
    records = [
        {
            "id": "r1",
            "inputs": {"question": "스토리지는 어떻게 고르나요?", "history": []},
            "expectations": {
                "answerable": True,
                "answer": "모범 답안",
                "required_points": ["워크로드별 성능 비교"],
                "evidence": [{"url": "https://elice.io/ko/test", "quote": "근거"}],
            },
        },
        {
            "id": "r2",
            "inputs": {"question": "비공개 가격은?", "history": []},
            "expectations": {"answerable": False},
        },
        {
            "id": "r3",
            "inputs": {"question": "실패한 질문", "history": []},
            "expectations": {"answerable": True},
        },
    ]
    cases = [
        {
            "record_id": record["id"],
            "error": "generation_failed" if i == 2 else None,
            "trace_id": "trace-id",
            "result": {
                "status": "answered" if i == 0 else "insufficient_evidence",
                "answer": "워크로드를 비교하세요." if i == 0 else "알 수 없습니다.",
                "reason": None,
                "citations": [],
                "generation_model": "HIDDEN_MODEL",
                "retrieved_sources": [{"text": "HIDDEN_CUMULATIVE_CANDIDATE"}],
                "generation_sources": [
                    {
                        "url": "https://elice.io/ko/test",
                        "title": "스토리지",
                        "text": "최종 생성 근거",
                    }
                ],
            },
            "scores": {"HIDDEN_JUDGE": 0},
        }
        for i, record in enumerate(records)
    ]
    bundle = create_bundle(
        RUN,
        {"dataset_id": "gold", "dataset_digest": "d" * 64, "records": records},
        {"generation_model": "HIDDEN_MODEL"},
        cases,
    )
    writes = []

    class Page(list):
        token = None

    class Tracking:
        fail = False
        fail_create = False
        runs = {}
        metrics = []

        def get_experiment_by_name(self, name):
            return SimpleNamespace(experiment_id="exp")

        def get_run(self, run_id):
            return self.runs.get(run_id) or SimpleNamespace(
                info=SimpleNamespace(
                    experiment_id="exp", status="FINISHED", run_id=run_id, start_time=123
                ),
                data=SimpleNamespace(
                    tags={
                        "activity": "evaluation",
                        "mlflow.runName": "sample",
                        "evaluation.schema": SCHEMA,
                        "evaluation.kind": "answers",
                    },
                    params={"dataset_id": "gold"},
                ),
            )

        def search_runs(self, experiments, **kw):
            query = kw.get("filter_string", "")
            if "grading_id" in query:
                key = query.split("'")[1]
                return Page([r for r in self.runs.values() if r.data.tags.get("grading_id") == key])
            return Page([self.get_run(RUN)])

        def create_run(self, experiment_id, *, tags):
            if self.fail_create:
                raise RuntimeError("unavailable")
            run_id = uuid4().hex
            run = SimpleNamespace(
                info=SimpleNamespace(
                    experiment_id=experiment_id, run_id=run_id, status="RUNNING", start_time=123
                ),
                data=SimpleNamespace(tags=tags.copy(), params={}),
            )
            self.runs[run_id] = run
            return run

        def download_artifacts(self, run_id, name, directory):
            path = Path(directory) / name
            path.write_text(json.dumps(bundle))
            return str(path)

        def log_dict(self, run_id, data, path):
            if self.fail:
                raise RuntimeError("secret MLflow endpoint")
            writes.append((run_id, deepcopy(data), path))

        def set_tag(self, run_id, key, value):
            self.runs[run_id].data.tags[key] = value

        def log_metric(self, run_id, name, value, **kw):
            self.metrics.append((run_id, name, value, kw))

        def set_terminated(self, run_id, state):
            self.runs[run_id].info.status = state

    tracking = Tracking()
    service.settings.mlflow_enabled = True
    monkeypatch.setattr("elice_knowledge.human_review.client", lambda _: tracking)
    monkeypatch.setattr("elice_knowledge.grading.client", lambda _: tracking)
    admin = browser(service, "test-admin-login-code")
    started = admin.post("/api/reviews", json={"run_id": RUN})
    assert started.status_code == 201
    grading_id = started.json()["grading_id"]
    return SimpleNamespace(
        service=service,
        admin=admin,
        tracking=tracking,
        bundle=bundle,
        writes=writes,
        grading_id=grading_id,
        path=f"/api/reviews/{RUN}/gradings/{grading_id}",
    )


def submission(case, score=2):
    return dict(
        answer_digest=case["answer_digest"],
        scores={
            e["dimension"]: dict(
                score=None if e["forced_na"] else score,
                status="not_applicable" if e["forced_na"] else "scored",
                rationale="근거와 공통 rubric을 확인했습니다.",
            )
            for e in case["dimensions"]
        },
    )


def finish_round(client, number, score=2):
    own = next(r for r in client.get("/api/reviews").json()["items"] if r["round"] == number)
    path = f"/api/reviews/{RUN}/gradings/{own['grading_id']}"
    for position in range(own["total"]):
        case = client.get(path + f"/cases/{position}").json()
        assert (
            client.post(path + f"/cases/{position}", json=submission(case, score)).status_code
            == 200
        )
    response = client.post(path + "/sync")
    assert response.status_code == 200
    return response.json()


def test_review_is_blind_owned_and_uses_shared_ordinal_anchors(review):
    anonymous = TestClient(create_app(review.service.settings))
    assert anonymous.get("/api/reviews").status_code == 401
    guest = browser(review.service, review.service.test_guest_code)
    assert guest.get(review.path + "/cases/0").status_code == 404
    assert (
        guest.post(
            "/api/reviews", json={"run_id": RUN, "reviewer_id": str(review.service.test_admin_id)}
        ).status_code
        == 422
    )
    case = review.admin.get(review.path + "/cases/0").json()
    assert "HIDDEN" not in json.dumps(case)
    assert case["dimensions"][0]["scale"] == [0, 1, 2]
    assert set(case["dimensions"][0]["rubric"]["levels"]) == {"0", "1", "2"}
    grounding = next(d for d in case["dimensions"] if d["dimension"] == "groundedness")
    assert "expectations" not in grounding
    assert grounding["outputs"]["generation_sources"][0]["text"] == "최종 생성 근거"
    assert review.admin.get(review.path + "/ratings").json()["ratings"] == []


def test_zero_is_valid_and_submission_is_atomic_immutable_and_idempotent(review):
    client, path = review.admin, review.path + "/cases/0"
    case = client.get(path).json()
    body = submission(case, 0)
    assert client.post(path, json={**body, "answer_digest": "0" * 64}).status_code == 409
    assert (
        client.post(
            path, json={**body, "scores": {"correctness": body["scores"]["correctness"]}}
        ).status_code
        == 422
    )
    assert client.post(path, json=body).status_code == 200
    assert client.post(path, json=body).status_code == 200
    body["scores"]["correctness"]["score"] = 1
    assert client.post(path, json=body).status_code == 409
    saved = client.get(path).json()["saved"]["ratings"]
    assert len(saved) == 4 and {r["score"] for r in saved} == {0}
    assert (
        browser(review.service, "test-admin-login-code")
        .get("/api/reviews")
        .json()["items"][0]["completed"]
        == 1
    )


@pytest.mark.parametrize("score", [True, False, 3, -1, 0.5, "2"])
def test_rating_score_rejects_wrong_type_or_range(review, score):
    case = review.admin.get(review.path + "/cases/0").json()
    body = submission(case)
    body["scores"]["correctness"]["score"] = score
    assert review.admin.post(review.path + "/cases/0", json=body).status_code == 422
    assert review.admin.get("/api/reviews").json()["items"][0]["completed"] == 0


def test_applicability_and_csrf_are_enforced(review):
    path = review.path + "/cases/1"
    case = review.admin.get(path).json()
    body = submission(case)
    assert {e["dimension"] for e in case["dimensions"] if e["forced_na"]} == {
        "correctness",
        "completeness",
    }
    body["scores"]["correctness"].update(status="scored", score=2)
    assert review.admin.post(path, json=body).status_code == 422
    body = submission(case)
    body["scores"]["relevance"].update(status="not_applicable", score=None)
    assert review.admin.post(path, json=body).status_code == 422
    assert (
        review.admin.post(
            path, json=submission(case), headers={"X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    assert review.admin.post(path, json=submission(case)).status_code == 200


def test_drafts_keep_zero_and_pending_scores_and_prevent_revision_conflicts(review):
    path = review.path + "/cases/0"
    case = review.admin.get(path).json()
    scores = {
        e["dimension"]: dict(status="pending", score=None, rationale="") for e in case["dimensions"]
    }
    scores["correctness"] = dict(status="scored", score=0, rationale="초안")
    body = dict(answer_digest=case["answer_digest"], revision=0, scores=scores)
    assert review.admin.put(path + "/draft", json=body).json()["revision"] == 1
    assert review.admin.put(path + "/draft", json=body).status_code == 409
    draft = review.admin.get(path).json()["draft"]
    assert draft["scores"]["correctness"]["score"] == 0
    final = submission(case)
    final["draft_revision"] = 0
    assert review.admin.post(path, json=final).status_code == 409
    final["draft_revision"] = 1
    assert review.admin.post(path, json=final).status_code == 200
    assert review.admin.get(path).json()["draft"] is None
    assert review.admin.put(path + "/draft", json={**body, "revision": 1}).status_code == 409


def test_repeated_rounds_and_tracking_retry_preserve_user_scores(review):
    pending = review.admin.post("/api/reviews", json={"run_id": RUN}).json()
    assert pending["grading_id"] == review.grading_id
    case = review.admin.get(review.path + "/cases/0").json()
    assert review.admin.post(review.path + "/cases/0", json=submission(case, 1)).status_code == 200
    review.tracking.fail = True
    assert review.admin.post(review.path + "/sync").status_code == 503
    assert review.admin.get(review.path + "/cases/0").json()["saved"]["ratings"][0]["score"] == 1
    review.tracking.fail = False
    finish_round(review.admin, 1, 1)
    new = review.admin.post("/api/reviews", json={"run_id": RUN}).json()
    assert new["round"] == 2 and new["grading_id"] != review.grading_id
    path = f"/api/reviews/{RUN}/gradings/{new['grading_id']}"
    assert review.admin.get(path + "/cases/0").json()["saved"] is None
    assert review.admin.get(path + "/cases/0").json()["draft"] is None
    assert review.admin.get("/api/review-candidates").json()["items"][0]["my_completed_rounds"] == 1
    finish_round(review.admin, 2, 0)
    first = review.admin.get(review.path + "/result").json()
    second = review.admin.get(path + "/result").json()
    assert first["schema"] == "evaluation-v2" and first["contract"] == "rubric-v1"
    assert first["ratings"][0]["score"] == 1 and second["ratings"][0]["score"] == 0
    assert review.tracking.get_run(RUN).info.status == "FINISHED"


def test_rubric_snapshot_is_bound_to_grading_not_answer_and_ids_disambiguate_round_one(review):
    from elice_knowledge.evaluation_schema import human_profile
    from elice_knowledge.grading import Grading
    from elice_knowledge.rubrics import load_rubric

    old_case = review.admin.get(review.path + "/cases/0").json()
    rubric = load_rubric("gold")
    rubric["id"] = "gold-ordinal-experiment"
    rubric["dimensions"]["correctness"]["levels"]["1"] = "실험용 기준"
    profile = human_profile(review.bundle, rubric)
    grade = Grading(review.service).allocate(
        review.bundle,
        "human:" + str(review.service.test_admin_id),
        profile,
        reviewer_id=review.service.test_admin_id,
    )
    assert grade["round"] == 1
    path = f"/api/reviews/{RUN}/gradings/{grade['id']}"
    new_case = review.admin.get(path + "/cases/0").json()
    assert new_case["dimensions"][0]["rubric"]["levels"]["1"] == "실험용 기준"
    assert old_case["answer_digest"] == new_case["answer_digest"]
    assert (
        review.admin.get(review.path + "/cases/0").json()["dimensions"][0]["rubric"]["levels"]["1"]
        != "실험용 기준"
    )
    assert len(review.admin.get("/api/reviews").json()["items"]) == 2
