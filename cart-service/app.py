"""Cart service. Port 5002.

The store forwards add, view, checkout, and payment here. A successful
payment creates an order on the order service and redirects to
/order_success. The card charge is fake.
"""

import os
import sqlite3
from datetime import datetime

import requests
from flask import Flask, jsonify, redirect, request

app = Flask(__name__)
DB_PATH = os.environ.get("CART_DB_PATH", "/tmp/cart.db")
ORDER_SERVICE_URL = os.environ.get("ORDER_SERVICE_URL", "http://localhost:5001").rstrip("/")

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Cart</title>
<style>
body {{ background:#111; color:#eee; font-family:sans-serif; margin:2rem; }}
a, button {{ color:#e11; }}
table {{ border-collapse:collapse; width:100%; max-width:40rem; }}
td, th {{ border-bottom:1px solid #333; padding:.4rem; text-align:left; }}
input {{ background:#222; color:#eee; border:1px solid #444; padding:.3rem; }}
.error {{ color:#f66; }}
</style></head><body>
<p><a href="/">Back to the shop</a></p>
{body}
</body></html>
"""


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            album_id INTEGER,
            album_name TEXT NOT NULL,
            artist TEXT NOT NULL,
            price REAL NOT NULL,
            quantity INTEGER NOT NULL
        )
        """
    )
    return conn


def items_for(session_id):
    with db() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM items WHERE session_id = ? ORDER BY id", (session_id,)
        )]


def money(value):
    return f"{float(value):.2f}"


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "cart"})


@app.post("/add_to_cart")
def add_to_cart():
    session_id = request.form.get("session_id") or os.urandom(16).hex()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO items (session_id, album_id, album_name, artist, price, quantity)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                request.form.get("album_id"),
                request.form["album_name"],
                request.form["artist"],
                float(request.form["price"]),
                int(request.form.get("quantity") or 1),
            ),
        )
        conn.commit()
    return jsonify({"success": True, "session_id": session_id})


def view_cart_body(session_id):
    rows = items_for(session_id)
    if not rows:
        body = "<h1>Cart</h1><p>The cart is empty.</p>"
        return PAGE.format(body=body)
    lines = ["<h1>Cart</h1><table><tr><th>Album</th><th>Qty</th><th></th></tr>"]
    total = 0
    for row in rows:
        total += row["price"] * row["quantity"]
        lines.append(
            "<tr><td>{name}<br>{artist}</td><td>"
            "<form action=\"/update_quantity\" method=\"post\">"
            "<input type=\"hidden\" name=\"item_id\" value=\"{id}\">"
            "<input name=\"quantity\" value=\"{qty}\" size=\"3\">"
            "<button type=\"submit\">Update</button></form></td><td>"
            "<form action=\"/remove_item\" method=\"post\">"
            "<input type=\"hidden\" name=\"item_id\" value=\"{id}\">"
            "<button type=\"submit\">Remove</button></form></td></tr>".format(
                name=row["album_name"], artist=row["artist"], id=row["id"], qty=row["quantity"]
            )
        )
    lines.append("</table><p>Total ${}</p>".format(money(total)))
    lines.append('<p><a href="/checkout">Check out</a></p>')
    return PAGE.format(body="".join(lines))


@app.get("/")
def view_cart():
    return view_cart_body(request.args.get("session_id") or "")


@app.post("/update_quantity")
def update_quantity():
    session_id = request.form["session_id"]
    quantity = int(request.form["quantity"])
    with db() as conn:
        if quantity < 1:
            conn.execute(
                "DELETE FROM items WHERE id = ? AND session_id = ?",
                (request.form["item_id"], session_id),
            )
        else:
            conn.execute(
                "UPDATE items SET quantity = ? WHERE id = ? AND session_id = ?",
                (quantity, request.form["item_id"], session_id),
            )
        conn.commit()
    return view_cart_body(session_id)


@app.post("/remove_item")
def remove_item():
    session_id = request.form["session_id"]
    with db() as conn:
        conn.execute(
            "DELETE FROM items WHERE id = ? AND session_id = ?",
            (request.form["item_id"], session_id),
        )
        conn.commit()
    return view_cart_body(session_id)


@app.get("/checkout")
def checkout():
    session_id = request.args.get("session_id") or ""
    rows = items_for(session_id)
    if not rows:
        return PAGE.format(body="<h1>Checkout</h1><p>The cart is empty.</p>")
    total = sum(row["price"] * row["quantity"] for row in rows)
    body = """
    <h1>Checkout</h1>
    <p>Total ${total}. The charge is fake.</p>
    <form action="/process_payment" method="post">
      <p>Card <input name="card_number" required></p>
      <p>Expiry <input name="expiry" placeholder="MM/YY" required></p>
      <p>CVV <input name="cvv" required></p>
      <button type="submit">Pay</button>
    </form>
    """.format(total=money(total))
    return PAGE.format(body=body)


def card_ok(form):
    digits = "".join(ch for ch in (form.get("card_number") or "") if ch.isdigit())
    cvv = (form.get("cvv") or "").strip()
    expiry = (form.get("expiry") or "").strip()
    if not (13 <= len(digits) <= 19) or not (3 <= len(cvv) <= 4 and cvv.isdigit()):
        return False
    try:
        month, year = expiry.split("/")
        month, year = int(month), int(year)
        if year < 100:
            year += 2000
        now = datetime.utcnow()
        return 1 <= month <= 12 and (year > now.year or (year == now.year and month >= now.month))
    except ValueError:
        return False


@app.post("/process_payment")
def process_payment():
    session_id = request.form.get("session_id") or ""
    if not card_ok(request.form):
        return PAGE.format(body="<h1>Checkout</h1><p class=\"error\">Card rejected. Use 13–19 digits, a future MM/YY, and a 3–4 digit CVV.</p>"), 400
    rows = items_for(session_id)
    if not rows:
        return PAGE.format(body="<h1>Checkout</h1><p class=\"error\">The cart is empty.</p>"), 400
    payload = {
        "items": [
            {
                "album_id": row["album_id"],
                "album_name": row["album_name"],
                "artist": row["artist"],
                "quantity": row["quantity"],
                "price": row["price"],
            }
            for row in rows
        ]
    }
    response = requests.post(f"{ORDER_SERVICE_URL}/api/orders", json=payload, timeout=10)
    if response.status_code >= 400:
        return PAGE.format(body="<h1>Checkout</h1><p class=\"error\">Order service rejected the order.</p>"), 502
    with db() as conn:
        conn.execute("DELETE FROM items WHERE session_id = ?", (session_id,))
        conn.commit()
    return redirect("/order_success", code=303)


@app.get("/order_success")
def order_success():
    return PAGE.format(body="<h1>Order placed</h1><p>The charge was not real.</p><p><a href=\"/\">Back to the shop</a></p>")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002)
