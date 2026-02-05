from fastapi import Request
from db import get_conn


def get_user_by_identifier(identifier: str):
    identifier = identifier.strip().lower()
    conn = get_conn()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM users WHERE identifier=?", (identifier,)).fetchone()
    conn.close()
    return row


def get_or_create_user_id(identifier: str) -> int:
    identifier = identifier.strip().lower()
    conn = get_conn()
    cur = conn.cursor()

    row = cur.execute("SELECT id FROM users WHERE identifier=?", (identifier,)).fetchone()
    if row:
        conn.close()
        return int(row["id"])

    cur.execute("INSERT INTO users(identifier) VALUES(?)", (identifier,))
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return int(user_id)


def login_user(request: Request, user_id: int):
    request.session["user_id"] = int(user_id)


def logout_user(request: Request):
    request.session.pop("user_id", None)


def require_login(request: Request):
    return request.session.get("user_id")
