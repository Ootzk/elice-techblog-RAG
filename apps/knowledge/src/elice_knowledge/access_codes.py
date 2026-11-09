"""Durable code identities. Plaintext guest codes are returned only at issuance."""

import secrets
from uuid import uuid4

from psycopg.types.json import Jsonb

from elice_knowledge.service_store import ServiceError, token_hash


def initialize_access_codes(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS access_codes (
        id uuid PRIMARY KEY, code_hash text UNIQUE NOT NULL, label text NOT NULL,
        kind text NOT NULL CHECK(kind IN ('admin','guest')),
        requests_per_hour integer NOT NULL CHECK(requests_per_hour BETWEEN 1 AND 1000),
        created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz,
        revoked_at timestamptz, last_login_at timestamptz, last_login_ip text)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS access_code_events (
        id uuid PRIMARY KEY, access_code_id uuid NOT NULL REFERENCES access_codes(id),
        actor_id uuid REFERENCES access_codes(id), action text NOT NULL, details jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())""")
    conn.execute("""ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS
        access_code_id uuid REFERENCES access_codes(id)""")
    conn.execute("""ALTER TABLE chat_conversations ADD COLUMN IF NOT EXISTS
        access_code_id uuid REFERENCES access_codes(id)""")
    conn.execute("ALTER TABLE chat_conversations ALTER COLUMN session_id DROP NOT NULL")
    conn.execute("""CREATE INDEX IF NOT EXISTS conversations_code_owner
        ON chat_conversations(access_code_id,updated_at DESC,id)""")

    if not conn.execute("""SELECT 1 FROM pg_constraint WHERE
        conrelid='chat_conversations'::regclass AND conname='conversation_owner'""").fetchone():
        conn.execute("""ALTER TABLE chat_conversations ADD CONSTRAINT conversation_owner
            CHECK(num_nonnulls(session_id,access_code_id)=1)""")


PUBLIC_FIELDS = (
    "id,label,kind,requests_per_hour,created_at,expires_at,revoked_at,last_login_at,last_login_ip"
)


class AccessCodes:
    def __init__(self, store):
        self.store = store

    def event(self, conn, code_id, actor, action, details):
        conn.execute(
            """INSERT INTO access_code_events(id,access_code_id,actor_id,action,details)
            VALUES (%s,%s,%s,%s,%s)""",
            (uuid4(), code_id, actor, action, Jsonb(details)),
        )

    def bootstrap(self):
        code = self.store.settings.admin_access_code
        if not code:
            return
        if not self.store.settings.auth_session_secret:
            raise ServiceError("auth_session_secret_required", 503)
        with self.store.connection() as conn:
            row = conn.execute(
                """INSERT INTO access_codes
                (id,code_hash,label,kind,requests_per_hour) VALUES (%s,%s,'관리자','admin',30)
                ON CONFLICT(code_hash) DO NOTHING RETURNING id""",
                (uuid4(), token_hash(code.get_secret_value())),
            ).fetchone()
            if row:
                self.event(conn, row["id"], None, "bootstrap_admin", {})

    def issue(self, actor, label, days, limit):
        code = secrets.token_urlsafe(18)
        with self.store.connection() as conn:
            self.require_admin(conn, actor)
            row = conn.execute(
                f"""INSERT INTO access_codes
                (id,code_hash,label,kind,expires_at,requests_per_hour)
                VALUES (%s,%s,%s,'guest',now()+(%s * interval '1 day'),%s)
                RETURNING {PUBLIC_FIELDS}""",
                (uuid4(), token_hash(code), label, days, limit),
            ).fetchone()
            self.event(
                conn, row["id"], actor, "create", {"expires_days": days, "requests_per_hour": limit}
            )
        return {**row, "code": code}

    def require_admin(self, conn, actor):
        row = conn.execute(
            """SELECT id FROM access_codes WHERE id=%s AND kind='admin'
            AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>now()) FOR UPDATE""",
            (actor,),
        ).fetchone()
        if not row:
            raise ServiceError("admin_required", 403)

    def list(self, actor, status="all", page=1):
        with self.store.connection() as conn:
            self.require_admin(conn, actor)
            rows = conn.execute(f"""SELECT {",".join("a." + f for f in PUBLIC_FIELDS.split(","))},
                CASE WHEN a.revoked_at IS NOT NULL THEN 'revoked'
                    WHEN a.expires_at<=now() THEN 'expired'
                    WHEN a.expires_at<=now()+interval '3 days' THEN 'expiring'
                    ELSE 'active' END AS status,
                (SELECT count(*) FROM chat_turns t JOIN chat_conversations c
                    ON c.id=t.conversation_id WHERE c.access_code_id=a.id
                    AND t.created_at>now()-interval '1 hour') AS requests_last_hour
                FROM access_codes a ORDER BY a.created_at DESC,a.id""").fetchall()
        filtered = [r for r in rows if status == "all" or r["status"] == status]
        return {
            "items": filtered[(page - 1) * 30 : page * 30],
            "total": len(filtered),
            "page": page,
            "page_size": 30,
        }

    def extend(self, actor, code_id, days):
        with self.store.connection() as conn:
            self.require_admin(conn, actor)
            row = conn.execute(
                "SELECT * FROM access_codes WHERE id=%s FOR UPDATE", (code_id,)
            ).fetchone()
            if not row:
                raise ServiceError("not_found", 404)
            if row["kind"] != "guest" or row["revoked_at"]:
                raise ServiceError("code_cannot_be_extended", 409)
            changed = conn.execute(
                f"""UPDATE access_codes
                SET expires_at=GREATEST(now(),expires_at)+(%s * interval '1 day')
                WHERE id=%s RETURNING {PUBLIC_FIELDS}""",
                (days, code_id),
            ).fetchone()
            self.event(
                conn,
                code_id,
                actor,
                "extend",
                {
                    "days": days,
                    "previous_expires_at": row["expires_at"].isoformat(),
                    "expires_at": changed["expires_at"].isoformat(),
                },
            )
        return changed

    def revoke(self, actor, code_id):
        with self.store.connection() as conn:
            self.require_admin(conn, actor)
            row = conn.execute(
                "SELECT * FROM access_codes WHERE id=%s FOR UPDATE", (code_id,)
            ).fetchone()
            if not row:
                raise ServiceError("not_found", 404)
            if row["kind"] != "guest":
                raise ServiceError("admin_code_requires_cli", 409)
            if not row["revoked_at"]:
                conn.execute("UPDATE access_codes SET revoked_at=now() WHERE id=%s", (code_id,))
                self.event(conn, code_id, actor, "revoke", {})

    def revoke_admin(self, code_id):
        with self.store.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(718205322)")
            row = conn.execute(
                "SELECT * FROM access_codes WHERE id=%s FOR UPDATE", (code_id,)
            ).fetchone()
            if not row or row["kind"] != "admin":
                raise ServiceError("not_found", 404)
            if row["revoked_at"]:
                return
            count = conn.execute(
                "SELECT count(*) AS n FROM access_codes WHERE kind='admin' AND revoked_at IS NULL"
            ).fetchone()["n"]
            if count <= 1:
                raise ServiceError("last_admin_cannot_be_revoked", 409)
            conn.execute("UPDATE access_codes SET revoked_at=now() WHERE id=%s", (code_id,))
            self.event(conn, code_id, None, "cli_revoke_admin", {})
