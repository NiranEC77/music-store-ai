"""Order service. Port 5001. SQLite. The cart creates an order; the store lists them."""

import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, jsonify, request

app = Flask(__name__)
DB_PATH = os.environ.get("ORDER_DB_PATH", "/tmp/orders.db")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            album_id INTEGER,
            album_name TEXT NOT NULL,
            artist TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL
        )
        """
    )
    return conn


def order_row(conn, order_id):
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return None
    items = [
        dict(row)
        for row in conn.execute(
            "SELECT album_id, album_name, artist, quantity, price FROM items WHERE order_id = ?",
            (order_id,),
        )
    ]
    return {
        "id": order["id"],
        "status": order["status"],
        "created_at": order["created_at"],
        "items": items,
    }


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "orders"})


@app.get("/api/orders")
def list_orders():
    with db() as conn:
        rows = conn.execute("SELECT id FROM orders ORDER BY id DESC").fetchall()
        return jsonify([order_row(conn, row["id"]) for row in rows])


@app.get("/api/orders/<int:order_id>")
def get_order(order_id):
    with db() as conn:
        order = order_row(conn, order_id)
    if order is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(order)


@app.post("/api/orders")
def create_order():
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    if not items:
        return jsonify({"error": "items required"}), 400
    created = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO orders (status, created_at) VALUES (?, ?)",
            ("created", created),
        )
        order_id = cur.lastrowid
        for item in items:
            conn.execute(
                """
                INSERT INTO items (order_id, album_id, album_name, artist, quantity, price)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    item.get("album_id"),
                    item["album_name"],
                    item["artist"],
                    int(item["quantity"]),
                    float(item["price"]),
                ),
            )
        conn.commit()
        return jsonify(order_row(conn, order_id)), 201


@app.put("/api/orders/<int:order_id>/status")
def update_status(order_id):
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    if not status:
        return jsonify({"error": "status required"}), 400
    with db() as conn:
        cur = conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "not found"}), 404
        return jsonify(order_row(conn, order_id))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
