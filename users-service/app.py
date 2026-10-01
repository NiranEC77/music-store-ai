"""Users service. Port 5003. Admin login only. Shoppers do not get accounts."""

import os
import secrets
import sqlite3

from flask import Flask, jsonify, request

app = Flask(__name__)
DB_PATH = os.environ.get("USERS_DB_PATH", "/tmp/users.db")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "metal")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tokens (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            role TEXT NOT NULL
        )
        """
    )
    return conn


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "users"})


@app.post("/api/login")
def login():
    payload = request.get_json(silent=True) or {}
    username = payload.get("username") or ""
    password = payload.get("password") or ""
    if username != ADMIN_USER or password != ADMIN_PASSWORD:
        return jsonify({"success": False, "error": "Login failed"}), 401
    token = secrets.token_hex(16)
    with db() as conn:
        conn.execute(
            "INSERT INTO tokens (token, username, role) VALUES (?, ?, ?)",
            (token, username, "admin"),
        )
        conn.commit()
    return jsonify({
        "success": True,
        "token": token,
        "user": {"username": username, "role": "admin"},
    })


@app.post("/api/logout")
def logout():
    payload = request.get_json(silent=True) or {}
    token = payload.get("token") or ""
    with db() as conn:
        conn.execute("DELETE FROM tokens WHERE token = ?", (token,))
        conn.commit()
    return jsonify({"success": True})


@app.post("/api/verify")
def verify():
    payload = request.get_json(silent=True) or {}
    token = payload.get("token") or ""
    with db() as conn:
        row = conn.execute("SELECT username, role FROM tokens WHERE token = ?", (token,)).fetchone()
    if row is None:
        return jsonify({"valid": False})
    return jsonify({"valid": True, "user": {"username": row["username"], "role": row["role"]}})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5003)
