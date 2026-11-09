"""Blind, code-owned human assessments bound to frozen MLflow evaluation answers."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlsplit, urlunsplit

from psycopg import sql
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from elice_knowledge.agreement import review_packet
from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_bundle import validate_bundle
from elice_knowledge.judges import inapplicable
from elice_knowledge.service_store import ServiceError
from elice_knowledge.tracking import client


def initialize_reviews(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS human_review_sources (
        run_id text PRIMARY KEY, bundle jsonb NOT NULL, packet jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())""")
    conn.execute("""CREATE TABLE IF NOT EXISTS human_review_rounds (
        run_id text NOT NULL REFERENCES human_review_sources(run_id),
        reviewer_id uuid NOT NULL REFERENCES access_codes(id),
        round integer NOT NULL CHECK(round>0), created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(run_id,reviewer_id,round))""")
    # Preserve already submitted development reviews as the reviewer's first round.
    with conn.cursor(row_factory=tuple_row) as cursor:
        legacy = cursor.execute("SELECT to_regclass('human_review_assignments')").fetchone()[0]
    if legacy:
        conn.execute("""INSERT INTO human_review_rounds(run_id,reviewer_id,round,created_at)
            SELECT a.run_id,a.reviewer_id,1,coalesce(min(r.created_at),now())
            FROM human_review_assignments a LEFT JOIN human_reviews r
            ON r.run_id=a.run_id AND r.reviewer_id=a.reviewer_id
            GROUP BY a.run_id,a.reviewer_id ON CONFLICT DO NOTHING""")
    conn.execute("""CREATE TABLE IF NOT EXISTS human_reviews (
        run_id text NOT NULL, reviewer_id uuid NOT NULL, record_id text NOT NULL,
        round integer NOT NULL DEFAULT 1,
        labels jsonb NOT NULL, synced boolean NOT NULL DEFAULT false,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(run_id,reviewer_id,round,record_id),
        CONSTRAINT human_reviews_round_fkey FOREIGN KEY(run_id,reviewer_id,round)
        REFERENCES human_review_rounds(run_id,reviewer_id,round))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS human_review_drafts (
        run_id text NOT NULL, reviewer_id uuid NOT NULL, record_id text NOT NULL,
        round integer NOT NULL DEFAULT 1,
        scores jsonb NOT NULL, revision integer NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(run_id,reviewer_id,round,record_id),
        CONSTRAINT human_review_drafts_round_fkey FOREIGN KEY(run_id,reviewer_id,round)
        REFERENCES human_review_rounds(run_id,reviewer_id,round))""")
    for table in ("human_reviews", "human_review_drafts"):
        if not conn.execute(
            """SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema()
            AND table_name=%s AND column_name='round'""",
            (table,),
        ).fetchone():
            conn.execute(
                sql.SQL("ALTER TABLE {} ADD COLUMN round integer NOT NULL DEFAULT 1").format(
                    sql.Identifier(table)
                )
            )
            conn.execute(
                sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
                    sql.Identifier(table), sql.Identifier(table + "_pkey")
                )
            )
            conn.execute(
                sql.SQL(
                    "ALTER TABLE {} ADD PRIMARY KEY(run_id,reviewer_id,round,record_id)"
                ).format(sql.Identifier(table))
            )
            conn.execute(
                sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
                    sql.Identifier(table), sql.Identifier(table + "_run_id_reviewer_id_fkey")
                )
            )
            conn.execute(
                sql.SQL("""ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY(run_id,reviewer_id,round)
                         REFERENCES human_review_rounds(run_id,reviewer_id,round)""").format(
                    sql.Identifier(table), sql.Identifier(table + "_round_fkey")
                )
            )
    conn.execute("DROP TABLE IF EXISTS human_review_assignments")


class HumanReview:
    def __init__(self, store):
        self.store = store

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
                filter_string="tags.activity = 'evaluation'",
                order_by=["attributes.start_time DESC"],
                max_results=25,
                page_token=page_token,
            )
            dataset_names = {}
            rounds = self.sources(reviewer_id)
            items = []
            for run in runs:
                item = self.run_metadata(tracking, run, dataset_names)
                if not include_origin:
                    item.pop("url")
                    item.pop("run_name")
                own = [r for r in rounds if r["run_id"] == item["run_id"]]
                item["my_completed_rounds"] = sum(
                    r["total"] > 0 and r["completed"] == r["total"] for r in own
                )
                item["my_rounds"] = own
                items.append(item)
            return {
                "items": items,
                "next_page_token": runs.token,
            }
        except ServiceError:
            raise
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
                or run.data.tags.get("activity") != "evaluation"
            ):
                raise ServiceError("not_found", 404)
            return self.run_metadata(tracking, run, {})
        except ServiceError:
            raise
        except Exception:
            raise ServiceError("review_tracking_unavailable", 503) from None

    def import_run(self, run_id):
        with self.store.connection() as conn:
            existing = conn.execute(
                "SELECT run_id FROM human_review_sources WHERE run_id=%s", (run_id,)
            ).fetchone()
        if not existing:
            try:
                tracking = self.tracking()
                run = tracking.get_run(run_id)
                if (
                    run.info.experiment_id != self.experiment(tracking)
                    or run.data.tags.get("activity") != "evaluation"
                    or run.info.status not in {"FINISHED", "FAILED", "KILLED"}
                ):
                    raise ServiceError("review_source_not_ready", 409)
                with TemporaryDirectory(prefix="human-review-") as directory:
                    path = tracking.download_artifacts(run_id, "evaluation-bundle.json", directory)
                    bundle = validate_bundle(json.loads(Path(path).read_text(encoding="utf-8")))
                if bundle["source_run_id"] != run_id:
                    raise ValueError("Source run mismatch")
                packet, _ = review_packet(bundle, "unused")
                # Pin rubric text and per-case applicability at import time.
                records = {r["id"]: r for r in bundle["records"]}
                cases = {c["record_id"]: c for c in bundle["cases"]}
                for entry in packet:
                    entry["forced_na"] = inapplicable(
                        records[entry["record_id"]], cases[entry["record_id"]], entry["dimension"]
                    )
                    entry["protocol"] = digest(entry["rubric"])
            except ServiceError:
                raise
            except Exception:
                raise ServiceError("review_bundle_unavailable_or_invalid", 422) from None
            with self.store.connection() as conn:
                conn.execute(
                    """INSERT INTO human_review_sources(run_id,bundle,packet) VALUES (%s,%s,%s)
                    ON CONFLICT DO NOTHING""",
                    (run_id, Jsonb(bundle), Jsonb(packet)),
                )

    def start(self, run_id, reviewer_id):
        self.import_run(run_id)
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT packet FROM human_review_sources WHERE run_id=%s FOR UPDATE", (run_id,)
            ).fetchone()
            total = len({e["record_id"] for e in row["packet"]})
            if not total:
                raise ServiceError("review_no_answers", 422)
            previous = conn.execute(
                """SELECT max(round) AS round FROM human_review_rounds
                WHERE run_id=%s AND reviewer_id=%s""",
                (run_id, reviewer_id),
            ).fetchone()["round"]
            if previous:
                completed = conn.execute(
                    """SELECT count(*) AS n FROM human_reviews
                    WHERE run_id=%s AND reviewer_id=%s AND round=%s""",
                    (run_id, reviewer_id, previous),
                ).fetchone()["n"]
                if completed < total:
                    return {"run_id": run_id, "round": previous}
            number = (previous or 0) + 1
            conn.execute(
                "INSERT INTO human_review_rounds(run_id,reviewer_id,round) VALUES (%s,%s,%s)",
                (run_id, reviewer_id, number),
            )
        return {"run_id": run_id, "round": number}

    def sources(self, reviewer_id):
        with self.store.connection() as conn:
            rows = conn.execute(
                """SELECT s.run_id,s.bundle->>'kind' AS kind,
                s.bundle->>'dataset_id' AS dataset_id,a.round,a.created_at,
                (SELECT count(DISTINCT p->>'record_id') FROM jsonb_array_elements(s.packet) p)
                AS total,s.packet,
                (SELECT coalesce(array_agg(r.record_id),ARRAY[]::text[]) FROM human_reviews r
                WHERE r.run_id=s.run_id AND r.reviewer_id=a.reviewer_id
                AND r.round=a.round) AS submitted,
                (SELECT count(*) FROM human_reviews r WHERE r.run_id=s.run_id
                AND r.reviewer_id=a.reviewer_id AND r.round=a.round) AS completed,
                (SELECT count(*) FROM human_reviews r WHERE r.run_id=s.run_id
                AND r.reviewer_id=a.reviewer_id AND r.round=a.round AND r.synced) AS synced
                FROM human_review_sources s JOIN human_review_rounds a USING(run_id)
                WHERE a.reviewer_id=%s ORDER BY a.created_at DESC,a.round DESC""",
                (reviewer_id,),
            ).fetchall()
        for row in rows:
            ids = list(dict.fromkeys(e["record_id"] for e in row.pop("packet")))
            submitted = set(row.pop("submitted"))
            row["next_position"] = next(
                (i for i, key in enumerate(ids) if key not in submitted), max(0, len(ids) - 1)
            )
        return rows

    def source(self, run_id, reviewer_id, number):
        with self.store.connection() as conn:
            source = conn.execute(
                """SELECT s.* FROM human_review_sources s JOIN human_review_rounds a
                USING(run_id) WHERE s.run_id=%s AND a.reviewer_id=%s AND a.round=%s""",
                (run_id, reviewer_id, number),
            ).fetchone()
        if not source:
            raise ServiceError("not_found", 404)
        return source

    def case(self, run_id, reviewer_id, number, position):
        source = self.source(run_id, reviewer_id, number)
        ids = list(dict.fromkeys(e["record_id"] for e in source["packet"]))
        if position < 0 or position >= len(ids):
            raise ServiceError("not_found", 404)
        record_id = ids[position]
        entries = [e for e in source["packet"] if e["record_id"] == record_id]
        with self.store.connection() as conn:
            saved = conn.execute(
                """SELECT labels,synced FROM human_reviews
                WHERE run_id=%s AND reviewer_id=%s AND round=%s AND record_id=%s""",
                (run_id, reviewer_id, number, record_id),
            ).fetchone()
            draft = conn.execute(
                """SELECT scores,revision,updated_at FROM human_review_drafts
                WHERE run_id=%s AND reviewer_id=%s AND round=%s AND record_id=%s""",
                (run_id, reviewer_id, number, record_id),
            ).fetchone()
        # Explicit allowlist: never serialize bundle, Judge scores, profile or other reviewers.
        return {
            "record_id": record_id,
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
            submitted = {
                r["record_id"]: r["synced"]
                for r in conn.execute(
                    """SELECT record_id,synced FROM human_reviews
                    WHERE run_id=%s AND reviewer_id=%s AND round=%s""",
                    (run_id, reviewer_id, number),
                ).fetchall()
            }
            drafts = {
                r["record_id"]
                for r in conn.execute(
                    """SELECT record_id FROM human_review_drafts
                    WHERE run_id=%s AND reviewer_id=%s AND round=%s""",
                    (run_id, reviewer_id, number),
                ).fetchall()
            }
        return [
            {
                "position": i,
                "submitted": key in submitted,
                "synced": submitted.get(key, False),
                "draft": key in drafts,
            }
            for i, key in enumerate(ids)
        ]

    def save_draft(self, run_id, reviewer_id, number, position, answer_digest, revision, scores):
        case = self.case(run_id, reviewer_id, number, position)
        if case["answer_digest"] != answer_digest:
            raise ServiceError("review_answer_changed", 409)
        dimensions = {e["dimension"]: e for e in case["dimensions"]}
        if set(scores) != set(dimensions):
            raise ServiceError("review_dimensions_mismatch", 422)
        for dimension, entry in dimensions.items():
            label = scores[dimension]["label"]
            if (entry["forced_na"] and label != "not_applicable") or (
                dimension == "behavior" and label == "not_applicable"
            ):
                raise ServiceError("review_invalid_applicability", 422)
        with self.store.connection() as conn:
            # Serialize draft/final submissions for this identity, including multiple tabs.
            self.lock_round(conn, run_id, reviewer_id, number)
            if conn.execute(
                """SELECT 1 FROM human_reviews
                WHERE run_id=%s AND reviewer_id=%s AND round=%s AND record_id=%s""",
                (run_id, reviewer_id, number, case["record_id"]),
            ).fetchone():
                raise ServiceError("review_already_submitted", 409)
            previous = conn.execute(
                """SELECT revision FROM human_review_drafts
                WHERE run_id=%s AND reviewer_id=%s AND round=%s AND record_id=%s""",
                (run_id, reviewer_id, number, case["record_id"]),
            ).fetchone()
            if revision != (previous["revision"] if previous else 0):
                raise ServiceError("review_draft_conflict", 409)
            conn.execute(
                """INSERT INTO human_review_drafts
                (run_id,reviewer_id,round,record_id,scores,revision)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(run_id,reviewer_id,round,record_id)
                DO UPDATE SET scores=excluded.scores,revision=excluded.revision,updated_at=now()""",
                (run_id, reviewer_id, number, case["record_id"], Jsonb(scores), revision + 1),
            )
        return {"revision": revision + 1}

    @staticmethod
    def lock_round(conn, run_id, reviewer_id, number):
        conn.execute(
            """SELECT 1 FROM human_review_rounds
            WHERE run_id=%s AND reviewer_id=%s AND round=%s FOR UPDATE""",
            (run_id, reviewer_id, number),
        ).fetchone()

    def save(
        self, run_id, reviewer_id, number, position, answer_digest, scores, draft_revision=None
    ):
        case = self.case(run_id, reviewer_id, number, position)
        if case["answer_digest"] != answer_digest:
            raise ServiceError("review_answer_changed", 409)
        dimensions = {e["dimension"]: e for e in case["dimensions"]}
        if set(scores) != set(dimensions):
            raise ServiceError("review_dimensions_mismatch", 422)
        labels = []
        for dimension, entry in dimensions.items():
            label = scores[dimension]["label"]
            if (entry["forced_na"] and label != "not_applicable") or (
                dimension == "behavior" and label == "not_applicable"
            ):
                raise ServiceError("review_invalid_applicability", 422)
            labels.append(
                {
                    "record_id": case["record_id"],
                    "answer_digest": answer_digest,
                    "dimension": dimension,
                    "rater": "human:" + str(reviewer_id),
                    "round": str(number),
                    "label": label,
                    "rationale": scores[dimension]["rationale"].strip(),
                    "protocol": entry["protocol"],
                }
            )
        with self.store.connection() as conn:
            self.lock_round(conn, run_id, reviewer_id, number)
            draft = conn.execute(
                """SELECT revision FROM human_review_drafts
                WHERE run_id=%s AND reviewer_id=%s AND round=%s AND record_id=%s""",
                (run_id, reviewer_id, number, case["record_id"]),
            ).fetchone()
            if draft and draft["revision"] != draft_revision:
                raise ServiceError("review_draft_conflict", 409)
            conn.execute(
                """INSERT INTO human_reviews(run_id,reviewer_id,round,record_id,labels)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (run_id, reviewer_id, number, case["record_id"], Jsonb(labels)),
            )
            row = conn.execute(
                """SELECT labels FROM human_reviews
                WHERE run_id=%s AND reviewer_id=%s AND round=%s AND record_id=%s""",
                (run_id, reviewer_id, number, case["record_id"]),
            ).fetchone()
            if row["labels"] != labels:
                raise ServiceError("review_already_submitted", 409)
            conn.execute(
                """DELETE FROM human_review_drafts
                WHERE run_id=%s AND reviewer_id=%s AND round=%s AND record_id=%s""",
                (run_id, reviewer_id, number, case["record_id"]),
            )
        return {"saved": True}

    def labels(self, run_id, reviewer_id, number):
        self.source(run_id, reviewer_id, number)
        with self.store.connection() as conn:
            return conn.execute(
                """SELECT record_id,labels,synced FROM human_reviews
                WHERE run_id=%s AND reviewer_id=%s AND round=%s ORDER BY record_id""",
                (run_id, reviewer_id, number),
            ).fetchall()

    def sync(self, run_id, reviewer_id, number):
        source = self.source(run_id, reviewer_id, number)
        rows = self.labels(run_id, reviewer_id, number)
        pending = [r for r in rows if not r["synced"]]
        try:
            tracking = self.tracking() if pending else None
            for row in pending:
                tracking.log_dict(
                    run_id,
                    {
                        "source_run_id": run_id,
                        "source_digest": source["bundle"]["digest"],
                        "labels": row["labels"],
                    },
                    f"human-review/{reviewer_id}/round-{number}/{digest(row['record_id'])}.json",
                )
                with self.store.connection() as conn:
                    conn.execute(
                        """UPDATE human_reviews SET synced=true
                        WHERE run_id=%s AND reviewer_id=%s AND round=%s AND record_id=%s""",
                        (run_id, reviewer_id, number, row["record_id"]),
                    )
        except Exception:
            raise ServiceError("review_sync_failed", 503) from None
        return {"synced": len(pending)}
