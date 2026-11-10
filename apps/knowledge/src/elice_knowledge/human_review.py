"""Blind human rounds using the shared evaluation-v1 grading registry."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlsplit, urlunsplit

from psycopg.types.json import Jsonb

from elice_knowledge.evaluation_bundle import SCHEMA, validate_bundle
from elice_knowledge.evaluation_schema import grading_result, human_profile
from elice_knowledge.grading import Grading, initialize_grading
from elice_knowledge.service_store import ServiceError
from elice_knowledge.tracking import client


def initialize_reviews(conn):
    initialize_grading(conn)


class HumanReview:
    def __init__(self, store):
        self.store = store
        self.grading = Grading(store)

    def tracking(self):
        if not self.store.settings.mlflow_enabled:
            raise ServiceError("review_tracking_unavailable", 503)
        return client(self.store.settings)

    def experiment(self, tracking):
        experiment = tracking.get_experiment_by_name(self.store.settings.qa_mlflow_experiment_name)
        if not experiment:
            raise ServiceError("review_experiment_not_found", 404)
        return experiment.experiment_id

    def candidates(self, reviewer_id, page_token=None, *, include_origin=False):
        try:
            tracking = self.tracking()
            runs = tracking.search_runs(
                [self.experiment(tracking)],
                filter_string=(
                    f"tags.`evaluation.schema` = '{SCHEMA}' AND tags.`evaluation.kind` = 'answers'"
                ),
                order_by=["attributes.start_time DESC"],
                max_results=25,
                page_token=page_token,
            )
            rounds = self.sources(reviewer_id)
            names, items = {}, []
            for run in runs:
                item = self.run_metadata(tracking, run, names)
                if not include_origin:
                    item.pop("url")
                    item.pop("run_name")
                own = [r for r in rounds if r["run_id"] == item["run_id"]]
                item["my_completed_rounds"] = sum(
                    r["total"] > 0 and r["completed"] == r["total"] for r in own
                )
                item["my_rounds"] = own
                items.append(item)
            return {"items": items, "next_page_token": runs.token}
        except ServiceError:
            raise
        except Exception:
            raise ServiceError("review_tracking_unavailable", 503) from None

    def import_run(self, run_id):
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT bundle FROM evaluation_sources WHERE run_id=%s", (run_id,)
            ).fetchone()
        if row:
            return validate_bundle(row["bundle"])
        try:
            tracking = self.tracking()
            self.origin(run_id)
            if tracking.get_run(run_id).info.status not in {"FINISHED", "FAILED", "KILLED"}:
                raise ServiceError("review_source_not_ready", 409)
            with TemporaryDirectory(prefix="human-review-") as directory:
                path = tracking.download_artifacts(run_id, "evaluation-bundle.json", directory)
                bundle = validate_bundle(json.loads(Path(path).read_text(encoding="utf-8")))
            if bundle["source_run_id"] != run_id:
                raise ValueError("Source mismatch")
            self.grading.register_source(bundle)
            return bundle
        except ServiceError:
            raise
        except Exception:
            raise ServiceError("review_bundle_unavailable_or_invalid", 422) from None

    def start(self, run_id, reviewer_id):
        bundle = self.import_run(run_id)
        if not any(not c["error"] for c in bundle["cases"]):
            raise ServiceError("review_no_answers", 422)
        grading = self.grading.allocate(
            bundle,
            "human:" + str(reviewer_id),
            human_profile(bundle),
            reviewer_id=reviewer_id,
            resume=True,
        )
        try:
            remote = self.grading.ensure_mlflow(grading["id"])
        except Exception:
            remote = None
        return {
            "run_id": run_id,
            "round": grading["round"],
            "grading_run_id": remote,
            "tracking_pending": remote is None,
        }

    def sources(self, reviewer_id):
        with self.store.connection() as conn:
            rows = conn.execute(
                """SELECT s.run_id,s.bundle->>'kind' AS kind,s.bundle->>'dataset_id' AS dataset_id,
                g.round,g.created_at,g.mlflow_run_id AS grading_run_id,g.state AS grading_state,
                s.packet,(SELECT coalesce(array_agg(c.record_id),ARRAY[]::text[])
                FROM grading_cases c WHERE c.grading_id=g.id) AS submitted,
                (SELECT count(*) FROM grading_cases c
                WHERE c.grading_id=g.id AND c.synced) AS synced
                FROM evaluation_sources s JOIN grading_runs g ON g.source_run_id=s.run_id
                WHERE g.reviewer_id=%s AND g.rater_type='human' ORDER BY g.created_at DESC""",
                (reviewer_id,),
            ).fetchall()
        for row in rows:
            ids = list(dict.fromkeys(e["record_id"] for e in row.pop("packet")))
            submitted = set(row.pop("submitted"))
            row.update(
                total=len(ids),
                completed=len(submitted),
                next_position=next(
                    (i for i, key in enumerate(ids) if key not in submitted), max(0, len(ids) - 1)
                ),
            )
        return rows

    def source(self, run_id, reviewer_id, number):
        with self.store.connection() as conn:
            grading = conn.execute(
                """SELECT * FROM grading_runs WHERE source_run_id=%s AND reviewer_id=%s
                AND rater_type='human' AND round=%s""",
                (run_id, reviewer_id, number),
            ).fetchone()
            if not grading:
                raise ServiceError("not_found", 404)
            source = conn.execute(
                "SELECT * FROM evaluation_sources WHERE run_id=%s", (run_id,)
            ).fetchone()
        return {**source, "grading": grading}

    def case(self, run_id, reviewer_id, number, position):
        source = self.source(run_id, reviewer_id, number)
        ids = list(dict.fromkeys(e["record_id"] for e in source["packet"]))
        if not 0 <= position < len(ids):
            raise ServiceError("not_found", 404)
        entries = [e for e in source["packet"] if e["record_id"] == ids[position]]
        with self.store.connection() as conn:
            saved = conn.execute(
                "SELECT labels,synced FROM grading_cases WHERE grading_id=%s AND record_id=%s",
                (source["grading"]["id"], ids[position]),
            ).fetchone()
            draft = conn.execute(
                """SELECT scores,revision,updated_at FROM grading_drafts
                WHERE grading_id=%s AND record_id=%s""",
                (source["grading"]["id"], ids[position]),
            ).fetchone()
        return {
            "record_id": ids[position],
            "answer_digest": entries[0]["answer_digest"],
            "position": position,
            "total": len(ids),
            "dimensions": entries,
            "saved": saved,
            "draft": draft if not saved else None,
        }

    def progress(self, run_id, reviewer_id, number):
        source = self.source(run_id, reviewer_id, number)
        ids = list(dict.fromkeys(e["record_id"] for e in source["packet"]))
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT record_id,synced FROM grading_cases WHERE grading_id=%s",
                (source["grading"]["id"],),
            ).fetchall()
            drafts = conn.execute(
                "SELECT record_id FROM grading_drafts WHERE grading_id=%s",
                (source["grading"]["id"],),
            ).fetchall()
        submitted = {r["record_id"]: r["synced"] for r in rows}
        return [
            {
                "position": i,
                "submitted": key in submitted,
                "synced": submitted.get(key, False),
                "draft": key in {r["record_id"] for r in drafts},
            }
            for i, key in enumerate(ids)
        ]

    def _validate_scores(self, run_id, reviewer_id, number, position, answer_digest, scores):
        case = self.case(run_id, reviewer_id, number, position)
        if case["answer_digest"] != answer_digest:
            raise ServiceError("review_answer_changed", 409)
        if set(scores) != {e["dimension"] for e in case["dimensions"]}:
            raise ServiceError("review_dimensions_mismatch", 422)
        for entry in case["dimensions"]:
            label = scores[entry["dimension"]]["label"]
            if (entry["forced_na"] and label != "not_applicable") or (
                entry["dimension"] == "behavior" and label == "not_applicable"
            ):
                raise ServiceError("review_invalid_applicability", 422)
        return case

    def save_draft(self, run_id, reviewer_id, number, position, answer_digest, revision, scores):
        case = self._validate_scores(run_id, reviewer_id, number, position, answer_digest, scores)
        grading = self.source(run_id, reviewer_id, number)["grading"]
        with self.store.connection() as conn:
            conn.execute("SELECT id FROM grading_runs WHERE id=%s FOR UPDATE", (grading["id"],))
            if conn.execute(
                "SELECT 1 FROM grading_cases WHERE grading_id=%s AND record_id=%s",
                (grading["id"], case["record_id"]),
            ).fetchone():
                raise ServiceError("review_already_submitted", 409)
            previous = conn.execute(
                "SELECT revision FROM grading_drafts WHERE grading_id=%s AND record_id=%s",
                (grading["id"], case["record_id"]),
            ).fetchone()
            if revision != (previous["revision"] if previous else 0):
                raise ServiceError("review_draft_conflict", 409)
            conn.execute(
                """INSERT INTO grading_drafts(grading_id,record_id,scores,revision)
                VALUES (%s,%s,%s,%s) ON CONFLICT(grading_id,record_id)
                DO UPDATE SET scores=excluded.scores,revision=excluded.revision,updated_at=now()""",
                (grading["id"], case["record_id"], Jsonb(scores), revision + 1),
            )
        return {"revision": revision + 1}

    def save(
        self, run_id, reviewer_id, number, position, answer_digest, scores, draft_revision=None
    ):
        case = self._validate_scores(run_id, reviewer_id, number, position, answer_digest, scores)
        grading = self.source(run_id, reviewer_id, number)["grading"]
        labels = [
            {
                "record_id": case["record_id"],
                "answer_digest": answer_digest,
                "dimension": e["dimension"],
                "rater": grading["rater_id"],
                "round": str(number),
                "label": scores[e["dimension"]]["label"],
                "rationale": scores[e["dimension"]]["rationale"].strip(),
                "protocol": grading["profile_digest"],
            }
            for e in case["dimensions"]
        ]
        with self.store.connection() as conn:
            conn.execute("SELECT id FROM grading_runs WHERE id=%s FOR UPDATE", (grading["id"],))
            draft = conn.execute(
                "SELECT revision FROM grading_drafts WHERE grading_id=%s AND record_id=%s",
                (grading["id"], case["record_id"]),
            ).fetchone()
            if draft and draft["revision"] != draft_revision:
                raise ServiceError("review_draft_conflict", 409)
            conn.execute(
                """INSERT INTO grading_cases(grading_id,record_id,labels)
                VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                (grading["id"], case["record_id"], Jsonb(labels)),
            )
            previous = conn.execute(
                "SELECT labels FROM grading_cases WHERE grading_id=%s AND record_id=%s",
                (grading["id"], case["record_id"]),
            ).fetchone()["labels"]
            if previous != labels:
                raise ServiceError("review_already_submitted", 409)
            conn.execute(
                "DELETE FROM grading_drafts WHERE grading_id=%s AND record_id=%s",
                (grading["id"], case["record_id"]),
            )
        return {"saved": True}

    def labels(self, run_id, reviewer_id, number):
        source = self.source(run_id, reviewer_id, number)
        with self.store.connection() as conn:
            return conn.execute(
                """SELECT record_id,labels,synced FROM grading_cases
                WHERE grading_id=%s ORDER BY record_id""",
                (source["grading"]["id"],),
            ).fetchall()

    def sync(self, run_id, reviewer_id, number):
        source = self.source(run_id, reviewer_id, number)
        try:
            result = self.grading.publish(source["grading"]["id"])
            return {
                "synced": result["synced"],
                "grading_run_id": result["run_id"],
                "state": result["state"],
            }
        except Exception:
            raise ServiceError("review_sync_failed", 503) from None

    def result(self, run_id, reviewer_id, number):
        source = self.source(run_id, reviewer_id, number)
        rows = self.labels(run_id, reviewer_id, number)
        return grading_result(
            source["bundle"], source["grading"], [label for row in rows for label in row["labels"]]
        )

    def grading_origin(self, run_id, reviewer_id, number):
        grading = self.source(run_id, reviewer_id, number)["grading"]
        if not grading["mlflow_run_id"]:
            return None
        try:
            tracking = self.tracking()
            return self.run_metadata(tracking, tracking.get_run(grading["mlflow_run_id"]), {})
        except Exception:
            raise ServiceError("review_tracking_unavailable", 503) from None

    def run_metadata(self, tracking, run, dataset_names):
        dataset_id = run.data.params.get("dataset_id")
        if dataset_id and dataset_id not in dataset_names:
            try:
                dataset_names[dataset_id] = tracking.get_dataset(dataset_id).name
            except Exception:
                dataset_names[dataset_id] = dataset_id
        # Browser links must never carry Basic Auth or query-string credentials.
        raw = self.store.settings.mlflow_tracking_uri
        url = None
        if raw:
            parsed = urlsplit(raw.get_secret_value())
            host = parsed.hostname
            if host and parsed.scheme in {"http", "https"}:
                host = f"[{host}]" if ":" in host else host
                authority = host + (f":{parsed.port}" if parsed.port else "")
                fragment = (
                    f"/experiments/{quote(str(run.info.experiment_id), safe='')}"
                    f"/runs/{quote(run.info.run_id, safe='')}"
                )
                url = urlunsplit(
                    (parsed.scheme, authority, parsed.path.rstrip("/") + "/", "", fragment)
                )
        return {
            "run_id": run.info.run_id,
            "run_name": run.data.tags.get("mlflow.runName", "dataset-qa"),
            "status": run.info.status,
            "start_time": run.info.start_time,
            "dataset_id": dataset_id,
            "dataset_name": dataset_names.get(dataset_id, dataset_id),
            "url": url,
        }

    def origin(self, run_id):
        try:
            tracking = self.tracking()
            run = tracking.get_run(run_id)
            if (
                run.info.experiment_id != self.experiment(tracking)
                or run.data.tags.get("evaluation.schema") != SCHEMA
                or run.data.tags.get("evaluation.kind") != "answers"
            ):
                raise ServiceError("not_found", 404)
            return self.run_metadata(tracking, run, {})
        except ServiceError:
            raise
        except Exception:
            raise ServiceError("review_tracking_unavailable", 503) from None
