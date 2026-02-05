from fastapi import Request
from passlib.context import CryptContext
from db import get_conn

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return pwd_context.verify(password, hashed)


def create_user(email: str, password: str) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users(email, hashed_password) VALUES(?,?)",
        (email.lower().strip(), hash_password(password))
    )
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return int(user_id)


def get_user_by_email(email: str):
    conn = get_conn()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT * FROM users WHERE email=?",
        (email.lower().strip(),)
    ).fetchone()
    conn.close()
    return row


def login_user(request: Request, user_id: int):
    request.session["user_id"] = int(user_id)


def logout_user(request: Request):
    request.session.pop("user_id", None)


def require_login(request: Request):
    return request.session.get("user_id")
