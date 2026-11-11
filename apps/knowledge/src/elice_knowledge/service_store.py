"""PostgreSQL ownership, immutable QA profiles and durable work queue."""

import hashlib
import hmac
import secrets
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from elice_knowledge.corpus import digest
from elice_knowledge.database import connect
from elice_knowledge.prompts import PromptError, resolve


class ServiceError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def initialize_service(conn):
    from elice_knowledge.access_codes import initialize_access_codes

    # Code identities own sessions and conversations, so they come first.
    initialize_access_codes(conn)
    for statement in [
        """CREATE TABLE IF NOT EXISTS chat_sessions (
            id uuid PRIMARY KEY, token_hash text UNIQUE NOT NULL, csrf_hash text NOT NULL,
            access_code_id uuid NOT NULL REFERENCES access_codes(id),
            expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now())""",
        """CREATE TABLE IF NOT EXISTS chat_login_attempts (
            bucket text NOT NULL, window_start timestamptz NOT NULL, attempts integer NOT NULL,
            PRIMARY KEY(bucket,window_start))""",
        """
            CREATE TABLE IF NOT EXISTS qa_profiles ( key text PRIMARY KEY, config jsonb NOT
            NULL, created_at timestamptz NOT NULL DEFAULT now())
        """,
        """
            CREATE TABLE IF NOT EXISTS chat_conversations ( id uuid PRIMARY KEY,
            access_code_id uuid NOT NULL REFERENCES access_codes(id), profile_key text NOT
            NULL REFERENCES qa_profiles(key), title text NOT NULL DEFAULT '새 대화', created_at
            timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
            deleted_at timestamptz)
        """,
        """CREATE INDEX IF NOT EXISTS conversations_code_owner
            ON chat_conversations(access_code_id,updated_at DESC,id)""",
        """
            CREATE TABLE IF NOT EXISTS qa_jobs ( id uuid PRIMARY KEY, kind text NOT NULL
            CHECK(kind IN ('chat','evaluation')), subject_id uuid NOT NULL, status text NOT
            NULL DEFAULT 'queued', stage text NOT NULL DEFAULT 'queued', payload jsonb NOT
            NULL, result jsonb, error text, lease_owner uuid, lease_expires timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz)
        """,
        """CREATE UNIQUE INDEX IF NOT EXISTS one_active_chat_turn ON qa_jobs(subject_id)
            WHERE kind='chat' AND status IN ('queued','running')""",
        """
            CREATE TABLE IF NOT EXISTS chat_turns ( id uuid PRIMARY KEY REFERENCES qa_jobs(id),
            conversation_id uuid NOT NULL REFERENCES chat_conversations(id), request_id uuid
            NOT NULL, request_hash text NOT NULL, question text NOT NULL, created_at
            timestamptz NOT NULL DEFAULT now(), UNIQUE(conversation_id,request_id))
        """,
        """CREATE TABLE IF NOT EXISTS qa_runs (
            id uuid PRIMARY KEY, profile_key text NOT NULL REFERENCES qa_profiles(key),
            inputs jsonb NOT NULL, result jsonb, error text, duration_ms double precision NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now())""",
        """
            CREATE TABLE IF NOT EXISTS evaluation_runs ( id uuid PRIMARY KEY REFERENCES
            qa_jobs(id), dataset_id text NOT NULL, dataset_digest text NOT NULL, profile_key
            text NOT NULL REFERENCES qa_profiles(key), records jsonb NOT NULL, mlflow_run_id
            text, summary jsonb)
        """,
        """CREATE TABLE IF NOT EXISTS evaluation_cases (
            evaluation_id uuid NOT NULL REFERENCES evaluation_runs(id), record_id text NOT NULL,
            run_id uuid, status text NOT NULL, result jsonb, scores jsonb, error text,
            PRIMARY KEY(evaluation_id,record_id))""",
    ]:
        conn.execute(statement)
    from elice_knowledge.human_review import initialize_reviews

    initialize_reviews(conn)


PROFILE_FIELDS = [
    "generation_model",
    "generation_timeout_seconds",
    "generation_temperature",
    "generation_max_output_tokens",
    "generation_token_limit_field",
    "qa_max_context_tokens",
    "qa_history_pairs",
    "qa_history_tokens",
    "qa_agent_max_tool_calls",
]


def code_identity():
    from elice_knowledge.experiment_conditions import PACKAGES

    base = Path(__file__).parent
    return digest(
        {
            "sources": {
                name: token_hash((base / name).read_text())
                for name in [
                    "qa.py",
                    "agent.py",
                    "tools.py",
                    "sparse.py",
                    "data/bm25_user_words.tsv",
                    "generation.py",
                    "retrieval.py",
                    "embedding.py",
                    "execution.py",
                    "experiment_conditions.py",
                    "tracing.py",
                ]
            },
            "dependencies": {name: version(name) for name in PACKAGES},
        }
    )


def provider_identity(settings):
    return token_hash(
        settings.generation_base_url.get_secret_value()
        if settings.generation_base_url
        else "unconfigured"
    )


class ServiceStore:
    def __init__(self, settings):
        self.settings = settings

    def connection(self):
        conn = connect(self.settings)
        conn.row_factory = dict_row
        return conn

    def login(self, code, client_id):
        s = self.settings
        if not s.auth_session_secret:
            raise ServiceError("chat_not_configured", 503)
        with self.connection() as conn:
            attempts = conn.execute(
                """INSERT INTO chat_login_attempts(bucket,window_start,attempts)
                VALUES (%s,date_trunc('minute',now()),1) ON CONFLICT(bucket,window_start)
                DO UPDATE SET attempts=chat_login_attempts.attempts+1 RETURNING attempts""",
                (token_hash(client_id),),
            ).fetchone()["attempts"]
        if attempts > s.chat_login_attempts_per_minute:
            raise ServiceError("login_rate_limited", 429)
        token = secrets.token_urlsafe(32)
        csrf = self.csrf(token)
        with self.connection() as conn:
            identity = conn.execute(
                """SELECT * FROM access_codes WHERE code_hash=%s
                AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>now()) FOR UPDATE""",
                (token_hash(code),),
            ).fetchone()
            if not identity:
                raise ServiceError("invalid_access_code", 401)
            conn.execute(
                "UPDATE access_codes SET last_login_at=now(),last_login_ip=%s WHERE id=%s",
                (client_id, identity["id"]),
            )
            row = conn.execute(
                """INSERT INTO chat_sessions(id,token_hash,csrf_hash,expires_at,access_code_id)
                VALUES (%s,%s,%s,now()+(%s * interval '1 hour'),%s)
                RETURNING id,expires_at,access_code_id""",
                (
                    uuid4(),
                    token_hash(token),
                    token_hash(csrf),
                    s.chat_session_hours,
                    identity["id"],
                ),
            ).fetchone()
            row.update(principal_id=identity["id"], role=identity["kind"], label=identity["label"])
        return token, csrf, row

    def session(self, token):
        if not token:
            raise ServiceError("authentication_required", 401)
        with self.connection() as conn:
            row = conn.execute(
                """SELECT s.*,a.kind AS role,a.label,
                    a.id AS principal_id
                FROM chat_sessions s JOIN access_codes a ON a.id=s.access_code_id
                WHERE s.token_hash=%s AND s.expires_at>now()
                AND a.revoked_at IS NULL
                    AND (a.expires_at IS NULL OR a.expires_at>now())""",
                (token_hash(token),),
            ).fetchone()
        if row is None or not secrets.compare_digest(
            row["csrf_hash"], token_hash(self.csrf(token))
        ):
            raise ServiceError("session_expired", 401)
        return row

    def csrf(self, token):
        secret = self.settings.auth_session_secret
        if not secret:
            raise ServiceError("session_expired", 401)
        return hmac.new(secret.get_secret_value().encode(), token.encode(), "sha256").hexdigest()

    def logout(self, session_id):
        with self.connection() as conn:
            conn.execute("UPDATE chat_sessions SET expires_at=now() WHERE id=%s", (session_id,))

    def active_profile(self, index_key=None, top_k=5, prompt_selectors=None):
        """Freeze index, generation settings and resolved prompt content into a profile."""
        s = self.settings
        try:
            # Resolved before the DB transaction: the registry is a network call.
            prompts = resolve(s, prompt_selectors)
        except PromptError as exc:
            raise ServiceError(exc.code, 503 if exc.code == "prompt_unavailable" else 422) from None
        with self.connection() as conn:
            from elice_knowledge.retrieval import IndexSelectionError, select_index

            try:
                resolved_index = select_index(conn, s, explicit_key=index_key)
            except IndexSelectionError:
                raise ServiceError("ambiguous_qa_index", 409) from None
            if not resolved_index:
                raise ServiceError("no_active_qa_index", 503)
            from elice_knowledge.experiment_conditions import freeze_conditions

            conditions = freeze_conditions(conn, resolved_index, s)
            config = {name: getattr(s, name) for name in PROFILE_FIELDS}
            config.update(
                experiment_conditions=conditions,
                index_key=resolved_index,
                top_k=top_k,
                prompts={name: prompt.model_dump() for name, prompt in prompts.items()},
                code_identity=code_identity(),
                provider_identity=provider_identity(s),
                build_revision=s.qa_build_revision,
                build_dirty=s.qa_build_dirty,
            )
            key = digest(config)
            conn.execute(
                "INSERT INTO qa_profiles(key,config) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (key, Jsonb(config)),
            )
        return key

    def profile(self, key):
        with self.connection() as conn:
            row = conn.execute("SELECT config FROM qa_profiles WHERE key=%s", (key,)).fetchone()
        if not row:
            raise ServiceError("unknown_qa_profile", 404)
        if row["config"]["code_identity"] != code_identity():
            raise ServiceError("qa_profile_code_changed", 409)
        if row["config"].get("provider_identity") != provider_identity(self.settings):
            raise ServiceError("qa_profile_provider_changed", 409)
        return row["config"]

    def create_conversation(self, owner):
        profile = self.active_profile()
        with self.connection() as conn:
            return conn.execute(
                """INSERT INTO chat_conversations(id,access_code_id,profile_key)
                VALUES (%s,%s,%s) RETURNING *""",
                (uuid4(), owner, profile),
            ).fetchone()

    def conversations(self, owner, cursor=None, limit=30):
        with self.connection() as conn:
            return conn.execute(
                """
                    SELECT id,title,updated_at FROM chat_conversations
                    WHERE access_code_id=%s AND
                    deleted_at IS NULL AND (%s::timestamptz IS NULL OR updated_at<%s) ORDER BY
                    updated_at DESC,id LIMIT %s
                """,
                (owner, cursor, cursor, limit),
            ).fetchall()

    def conversation(self, owner, conversation_id, delete=False):
        with self.connection() as conn:
            row = conn.execute(
                (
                    "SELECT * FROM chat_conversations WHERE id=%s "
                    "AND access_code_id=%s AND "
                    "deleted_at IS NULL FOR UPDATE"
                ),
                (conversation_id, owner),
            ).fetchone()
            if not row:
                raise ServiceError("not_found", 404)
            if delete:
                active = conn.execute(
                    (
                        "SELECT 1 FROM qa_jobs WHERE subject_id=%s AND kind='chat' AND status "
                        "IN ('queued','running')"
                    ),
                    (conversation_id,),
                ).fetchone()
                if active:
                    raise ServiceError("conversation_busy", 409)
                conn.execute(
                    "UPDATE chat_conversations SET deleted_at=now() WHERE id=%s", (conversation_id,)
                )
                return None
            row["turns"] = conn.execute(
                """SELECT t.id,t.conversation_id,t.question,j.status,j.stage,j.result,j.error
                FROM chat_turns t JOIN qa_jobs j ON j.id=t.id WHERE t.conversation_id=%s
                ORDER BY t.created_at,t.id""",
                (conversation_id,),
            ).fetchall()
        return row

    def enqueue_turn(self, owner, conversation_id, request_id, question):
        fingerprint = digest({"question": question})
        with self.connection() as conn:
            # Serialize the quota across every browser using the same code.
            identity = conn.execute(
                "SELECT * FROM access_codes WHERE id=%s FOR UPDATE", (owner,)
            ).fetchone()
            if not identity:
                raise ServiceError("session_expired", 401)
            if (
                identity["revoked_at"]
                or conn.execute(
                    "SELECT expires_at<=now() AS expired FROM access_codes WHERE id=%s", (owner,)
                ).fetchone()["expired"]
            ):
                raise ServiceError("session_expired", 401)
            conv = conn.execute(
                (
                    "SELECT * FROM chat_conversations WHERE id=%s "
                    "AND access_code_id=%s AND "
                    "deleted_at IS NULL FOR UPDATE"
                ),
                (conversation_id, owner),
            ).fetchone()
            if not conv:
                raise ServiceError("not_found", 404)
            old = conn.execute(
                "SELECT * FROM chat_turns WHERE conversation_id=%s AND request_id=%s",
                (conversation_id, request_id),
            ).fetchone()
            if old:
                if old["request_hash"] != fingerprint:
                    raise ServiceError("request_id_conflict", 409)
                return str(old["id"])
            if conn.execute(
                (
                    "SELECT 1 FROM qa_jobs WHERE kind='chat' AND subject_id=%s AND status IN "
                    "('queued','running')"
                ),
                (conversation_id,),
            ).fetchone():
                raise ServiceError("conversation_busy", 409)
            used = conn.execute(
                """
                    SELECT count(*) AS n FROM chat_turns t JOIN chat_conversations c ON
                    c.id=t.conversation_id WHERE c.access_code_id=%s AND
                    t.created_at>now()-interval '1 hour'
                """,
                (owner,),
            ).fetchone()["n"]
            if used >= identity["requests_per_hour"] and identity["kind"] != "admin":
                raise ServiceError("chat_rate_limited", 429)
            job_id = uuid4()
            payload = {"question": question, "profile_key": conv["profile_key"]}
            conn.execute(
                "INSERT INTO qa_jobs(id,kind,subject_id,payload) VALUES (%s,'chat',%s,%s)",
                (job_id, conversation_id, Jsonb(payload)),
            )
            conn.execute(
                """
                    INSERT INTO chat_turns(id,conversation_id,request_id,request_hash,question)
                    VALUES (%s,%s,%s,%s,%s)
                """,
                (job_id, conversation_id, request_id, fingerprint, question),
            )
            conn.execute(
                """
                    UPDATE chat_conversations SET title=CASE WHEN title='새 대화' THEN %s ELSE
                    title END, updated_at=now() WHERE id=%s
                """,
                (question[:60], conversation_id),
            )
        return str(job_id)

    def turn(self, owner, turn_id):
        with self.connection() as conn:
            row = conn.execute(
                """
                    SELECT j.*,t.conversation_id,t.question FROM qa_jobs j JOIN chat_turns t ON
                    t.id=j.id JOIN chat_conversations c ON c.id=t.conversation_id WHERE j.id=%s
                    AND c.access_code_id=%s AND c.deleted_at IS NULL
                """,
                (turn_id, owner),
            ).fetchone()
        if not row:
            raise ServiceError("not_found", 404)
        return row

    def claim(self):
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(718205321)")
            conn.execute("""
                             UPDATE qa_jobs SET
                             status='interrupted',stage='interrupted',error='worker_interrupted',finished_at=now()
                             WHERE status='running' AND lease_expires<now()
                         """)
            conn.execute("""UPDATE qa_jobs j SET status='interrupted',stage='interrupted',
                error='access_code_inactive',finished_at=now()
                FROM chat_conversations c,access_codes a
                WHERE j.kind='chat' AND j.status='queued' AND j.subject_id=c.id
                AND c.access_code_id=a.id AND (a.revoked_at IS NOT NULL OR a.expires_at<=now())""")
            running = conn.execute(
                "SELECT count(*) AS n FROM qa_jobs WHERE status='running'"
            ).fetchone()["n"]
            if running >= self.settings.qa_worker_concurrency:
                return None
            row = conn.execute(
                "SELECT id FROM qa_jobs WHERE status='queued' ORDER BY created_at,id FOR "
                "UPDATE SKIP LOCKED LIMIT 1"
            ).fetchone()
            if not row:
                return None
            return conn.execute(
                """UPDATE qa_jobs SET status='running',stage='retrieving',lease_owner=%s,
                lease_expires=now()+(%s * interval '1 second') WHERE id=%s RETURNING *,
                (extract(epoch FROM (clock_timestamp()-created_at))*1000)::double precision
                AS queue_wait_ms""",
                (uuid4(), self.settings.worker_lease_seconds, row["id"]),
            ).fetchone()

    def heartbeat(self, job, stage):
        with self.connection() as conn:
            row = conn.execute(
                """UPDATE qa_jobs SET stage=%s,lease_expires=now()+(%s * interval '1 second')
                WHERE id=%s AND status='running' AND lease_owner=%s RETURNING id""",
                (stage, self.settings.worker_lease_seconds, job["id"], job["lease_owner"]),
            ).fetchone()
        if not row:
            raise ServiceError("job_lease_lost", 409)

    def finish(self, job, result=None, error=None):
        with self.connection() as conn:
            conn.execute(
                """UPDATE qa_jobs SET status=%s,stage=%s,result=%s,error=%s,finished_at=now()
                WHERE id=%s AND status='running' AND lease_owner=%s""",
                (
                    "failed" if error else "completed",
                    "failed" if error else "completed",
                    Jsonb(result) if result else None,
                    error,
                    job["id"],
                    job["lease_owner"],
                ),
            )

    def history(self, conversation_id):
        with self.connection() as conn:
            rows = conn.execute(
                """
                    SELECT t.question,j.result FROM chat_turns t JOIN qa_jobs j ON j.id=t.id
                    WHERE t.conversation_id=%s AND j.status='completed' ORDER BY t.created_at
                    DESC,t.id DESC LIMIT %s
                """,
                (conversation_id, 10),
            ).fetchall()
        return [
            message
            for row in reversed(rows)
            for message in [
                {"role": "user", "content": row["question"]},
                {
                    "role": "assistant",
                    "content": row["result"]["result"]["answer"][:16000]
                    or "근거가 부족하여 답변하지 못했습니다.",
                },
            ]
        ]

    def save_run(self, profile, inputs, result, error, duration):
        run_id = uuid4()
        with self.connection() as conn:
            conn.execute(
                (
                    "INSERT INTO qa_runs(id,profile_key,inputs,result,error,duration_ms) VALUES"
                    " (%s,%s,%s,%s,%s,%s)"
                ),
                (
                    run_id,
                    profile,
                    Jsonb(inputs),
                    Jsonb(result) if result else None,
                    error,
                    duration,
                ),
            )
        return str(run_id)

    def _delete_conversation(self, conn, conversation_id):
        turns = conn.execute(
            "DELETE FROM chat_turns WHERE conversation_id=%s RETURNING id", (conversation_id,)
        ).fetchall()
        ids = [turn["id"] for turn in turns]
        if ids:
            conn.execute(
                """DELETE FROM qa_runs WHERE id IN (
                SELECT (result->>'run_id')::uuid FROM qa_jobs WHERE id=ANY(%s))""",
                (ids,),
            )
            conn.execute("DELETE FROM qa_jobs WHERE id=ANY(%s)", (ids,))
        conn.execute("DELETE FROM chat_conversations WHERE id=%s", (conversation_id,))

    def cleanup(self):
        with self.connection() as conn:
            conn.execute(
                "DELETE FROM chat_login_attempts WHERE window_start<now()-interval '1 day'"
            )
            deleted = conn.execute("""SELECT id FROM chat_conversations
                WHERE deleted_at<now()-interval '1 day' FOR UPDATE SKIP LOCKED""").fetchall()
            for conversation in deleted:
                self._delete_conversation(conn, conversation["id"])
            # Conversations belong to codes, so expired sessions are removed alone.
            conn.execute("DELETE FROM chat_sessions WHERE expires_at<now()-interval '1 day'")
