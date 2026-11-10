"""One subject, one frozen answer set, one repetition per local and MLflow grading run."""

from uuid import uuid4

from psycopg.types.json import Jsonb

from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_bundle import SCHEMA, validate_bundle
from elice_knowledge.evaluation_schema import (
    grading_result,
    human_profile,
    metrics,
    model_profile,
    tags,
)
from elice_knowledge.service_store import ServiceError
from elice_knowledge.tracking import client


def initialize_grading(conn):
    for statement in [
        """CREATE TABLE IF NOT EXISTS evaluation_sources (
            run_id text PRIMARY KEY,bundle jsonb NOT NULL,packet jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now())""",
        """CREATE TABLE IF NOT EXISTS grading_runs (
            id uuid PRIMARY KEY,source_run_id text NOT NULL REFERENCES evaluation_sources(run_id),
            rater_type text NOT NULL CHECK(rater_type IN ('human','model')),rater_id text NOT NULL,
            reviewer_id uuid REFERENCES access_codes(id),profile jsonb NOT NULL,
            profile_digest text NOT NULL,round integer NOT NULL CHECK(round>0),
            mlflow_run_id text UNIQUE,sync_step integer NOT NULL DEFAULT 0,
            state text NOT NULL DEFAULT 'RUNNING',created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(source_run_id,rater_id,profile_digest,round),
            CHECK((rater_type='human')=(reviewer_id IS NOT NULL)))""",
        """CREATE TABLE IF NOT EXISTS grading_cases (
            grading_id uuid NOT NULL REFERENCES grading_runs(id),record_id text NOT NULL,
            labels jsonb NOT NULL,synced boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(grading_id,record_id))""",
        """CREATE TABLE IF NOT EXISTS grading_drafts (
            grading_id uuid NOT NULL REFERENCES grading_runs(id),record_id text NOT NULL,
            scores jsonb NOT NULL,revision integer NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(grading_id,record_id))""",
    ]:
        conn.execute(statement)


class Grading:
    def __init__(self, store):
        self.store = store

    def register_source(self, bundle):
        from elice_knowledge.agreement import review_packet
        from elice_knowledge.judges import inapplicable

        validate_bundle(bundle)
        packet, _ = review_packet(bundle, "unused")
        records = {r["id"]: r for r in bundle["records"]}
        cases = {c["record_id"]: c for c in bundle["cases"]}
        for entry in packet:
            entry["forced_na"] = inapplicable(
                records[entry["record_id"]], cases[entry["record_id"]], entry["dimension"]
            )
        with self.store.connection() as conn:
            conn.execute(
                """INSERT INTO evaluation_sources(run_id,bundle,packet) VALUES (%s,%s,%s)
                ON CONFLICT DO NOTHING""",
                (bundle["source_run_id"], Jsonb(bundle), Jsonb(packet)),
            )
            saved = conn.execute(
                "SELECT bundle FROM evaluation_sources WHERE run_id=%s", (bundle["source_run_id"],)
            ).fetchone()["bundle"]
            if saved["digest"] != bundle["digest"]:
                raise ServiceError("grading_source_changed", 409)

    def allocate(self, bundle, rater_id, profile, *, reviewer_id=None, resume=False):
        self.register_source(bundle)
        kind = "human" if reviewer_id is not None else "model"
        if not rater_id.startswith(kind + ":"):
            raise ValueError("Invalid rater identity")
        if profile.get("type") != kind or (
            reviewer_id is not None and rater_id != "human:" + str(reviewer_id)
        ):
            raise ValueError("Invalid rater profile identity")
        expected = (
            human_profile(bundle)
            if reviewer_id is not None
            else model_profile(bundle, profile.get("scorers", []))
        )
        if profile != expected or (
            reviewer_id is None and rater_id != "model:" + profile["scorers"][0]["rater"]
        ):
            raise ValueError("Invalid rater profile")
        profile_key = digest(profile)
        with self.store.connection() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (digest([bundle["source_run_id"], rater_id, profile_key]),),
            )
            last = conn.execute(
                """SELECT * FROM grading_runs WHERE source_run_id=%s AND rater_id=%s
                AND profile_digest=%s ORDER BY round DESC LIMIT 1""",
                (bundle["source_run_id"], rater_id, profile_key),
            ).fetchone()
            if resume and last:
                count = conn.execute(
                    "SELECT count(*) AS n FROM grading_cases WHERE grading_id=%s", (last["id"],)
                ).fetchone()["n"]
                if count < sum(not c["error"] for c in bundle["cases"]):
                    return last
            return conn.execute(
                """INSERT INTO grading_runs
                (id,source_run_id,rater_type,rater_id,reviewer_id,profile,profile_digest,round)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (
                    uuid4(),
                    bundle["source_run_id"],
                    kind,
                    rater_id,
                    reviewer_id,
                    Jsonb(profile),
                    profile_key,
                    last["round"] + 1 if last else 1,
                ),
            ).fetchone()

    def get(self, grading_id):
        with self.store.connection() as conn:
            return conn.execute("SELECT * FROM grading_runs WHERE id=%s", (grading_id,)).fetchone()

    def ensure_mlflow(self, grading_id):
        if not self.store.settings.mlflow_enabled:
            raise ServiceError("evaluation_tracking_not_configured", 503)
        tracking = client(self.store.settings)
        # A metadata call, not inference: lock only this grading during idempotent remote creation.
        with self.store.connection() as conn:
            grading = conn.execute(
                "SELECT * FROM grading_runs WHERE id=%s FOR UPDATE", (grading_id,)
            ).fetchone()
            if grading["mlflow_run_id"]:
                return grading["mlflow_run_id"]
            source = tracking.get_run(grading["source_run_id"])
            if (
                source.data.tags.get("evaluation.schema") != SCHEMA
                or source.data.tags.get("evaluation.kind") != "answers"
            ):
                raise ValueError("unsupported_source_run_schema")
            found = tracking.search_runs(
                [source.info.experiment_id],
                filter_string=f"tags.grading_id = '{grading['id']}'",
                max_results=2,
            )
            if len(found) > 1:
                raise RuntimeError("duplicate_remote_grading")
            if found and found[0].data.tags.get("grading_id") != str(grading_id):
                raise RuntimeError("grading_identity_mismatch")
            run_id = (
                found[0].info.run_id
                if found
                else tracking.create_run(
                    source.info.experiment_id,
                    tags=tags(
                        "grading",
                        **{
                            "mlflow.runName": f"{grading['rater_id']} · round {grading['round']}",
                            "mlflow.parentRunId": grading["source_run_id"],
                            "source_run_id": grading["source_run_id"],
                            "source_bundle_digest": source.data.tags.get(
                                "source_bundle_digest", ""
                            ),
                            "grading_id": str(grading["id"]),
                            "rater_type": grading["rater_type"],
                            "rater_id": grading["rater_id"],
                            "rater_profile_digest": grading["profile_digest"],
                            "round": str(grading["round"]),
                        },
                    ),
                ).info.run_id
            )
            bundle = conn.execute(
                "SELECT bundle FROM evaluation_sources WHERE run_id=%s", (grading["source_run_id"],)
            ).fetchone()["bundle"]
            tracking.set_tag(run_id, "source_bundle_digest", bundle["digest"])
            tracking.log_dict(run_id, grading["profile"], "rater-profile.json")
            conn.execute(
                "UPDATE grading_runs SET mlflow_run_id=%s WHERE id=%s", (run_id, grading_id)
            )
            return run_id

    def write_case(self, grading_id, record_id, labels):
        with self.store.connection() as conn:
            conn.execute("SELECT id FROM grading_runs WHERE id=%s FOR UPDATE", (grading_id,))
            conn.execute(
                """INSERT INTO grading_cases(grading_id,record_id,labels) VALUES (%s,%s,%s)
                ON CONFLICT DO NOTHING""",
                (grading_id, record_id, Jsonb(labels)),
            )
            saved = conn.execute(
                "SELECT labels FROM grading_cases WHERE grading_id=%s AND record_id=%s",
                (grading_id, record_id),
            ).fetchone()["labels"]
            if saved != labels:
                raise ServiceError("review_already_submitted", 409)

    def publish(self, grading_id):
        run_id = self.ensure_mlflow(grading_id)
        tracking = client(self.store.settings)
        with self.store.connection() as conn:
            grading = conn.execute(
                "SELECT * FROM grading_runs WHERE id=%s FOR UPDATE", (grading_id,)
            ).fetchone()
            bundle = conn.execute(
                "SELECT bundle FROM evaluation_sources WHERE run_id=%s", (grading["source_run_id"],)
            ).fetchone()["bundle"]
            rows = conn.execute(
                "SELECT * FROM grading_cases WHERE grading_id=%s ORDER BY record_id", (grading_id,)
            ).fetchall()
            result = grading_result(
                bundle, grading, [label for row in rows for label in row["labels"]]
            )
            values = metrics(bundle, result)
            if grading["sync_step"] and all(row["synced"] for row in rows):
                return {
                    "run_id": run_id,
                    "state": grading["state"],
                    "metrics": values,
                    "synced": 0,
                    "result": result,
                }
            tracking.log_dict(run_id, result, "grading-result.json")
            for name, value in values.items():
                tracking.log_metric(run_id, name, value, step=grading["sync_step"] + 1)
            state = "RUNNING"
            if result["complete"]:
                state = (
                    "FAILED" if any(r["label"] == "error" for r in result["labels"]) else "FINISHED"
                )
                tracking.set_terminated(run_id, state)
            conn.execute("UPDATE grading_cases SET synced=true WHERE grading_id=%s", (grading_id,))
            conn.execute(
                "UPDATE grading_runs SET state=%s,sync_step=sync_step+1 WHERE id=%s",
                (state, grading_id),
            )
        return {
            "run_id": run_id,
            "state": state,
            "metrics": values,
            "synced": sum(not row["synced"] for row in rows),
            "result": result,
        }
