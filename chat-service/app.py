"""Metal Oracle. Port 5005.

The store streams POST /api/chat to the browser. This service calls
tools for the live album catalog and recent orders, then asks the
private model to speak only those tool results. It does not invent a
title or a count.

POST /mcp is a small Model Context Protocol endpoint (initialize,
tools/list, tools/call) for the same tools.
"""

import json
import os

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "music_store")
DB_USER = os.environ.get("DB_USER", "music_user")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "music_password")
ORDER_SERVICE_URL = os.environ.get("ORDER_SERVICE_URL", "http://localhost:5001").rstrip("/")

TOOL_DEFS = [
    {
        "name": "album_count",
        "description": "How many albums are in the music store catalog right now.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_albums",
        "description": "Every album in the catalog: id, name, artist, and price.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_orders",
        "description": "Recent orders from the order service.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def db():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        connect_timeout=5,
    )


def _albums():
    with db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, name, artist, price FROM albums ORDER BY name")
            albums = [dict(row) for row in cur.fetchall()]
    for row in albums:
        if row.get("price") is not None:
            row["price"] = str(row["price"])
    return albums


def tool_album_count():
    return {"count": len(_albums())}


def tool_list_albums():
    albums = _albums()
    return {"count": len(albums), "albums": albums}


def tool_list_orders():
    response = requests.get(f"{ORDER_SERVICE_URL}/api/orders", timeout=5)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict):
        data = data.get("orders", data.get("items", []))
    if not isinstance(data, list):
        data = []
    return {"count": len(data), "orders": data[:20]}


TOOLS = {
    "album_count": tool_album_count,
    "list_albums": tool_list_albums,
    "list_orders": tool_list_orders,
}


def run_tool(name):
    if name not in TOOLS:
        raise KeyError(name)
    return TOOLS[name]()


def sse(obj):
    if obj == "[DONE]":
        return b"data: [DONE]\n\n"
    return f"data: {json.dumps(obj)}\n\n".encode()


def wants_count(message):
    text = (message or "").lower()
    return any(phrase in text for phrase in ("how many", "count", "number of"))


def wants_orders(message):
    text = (message or "").lower()
    return any(
        phrase in text for phrase in ("order", "bought", "purchase", "sold", "sale")
    )


def _cred_string(value):
    return value.strip() if isinstance(value, str) else ""


def _from_credentials(creds):
    if not isinstance(creds, dict):
        return None
    nested = creds.get("endpoint")
    if isinstance(nested, dict):
        creds = {**creds, **nested}
    key = _cred_string(creds.get("api_key") or creds.get("apiKey"))
    base = creds.get("api_base") or creds.get("url") or creds.get("base_url")
    if isinstance(base, dict):
        key = key or _cred_string(base.get("api_key") or base.get("apiKey"))
        base = base.get("api_base") or base.get("url")
    base = _cred_string(base).rstrip("/")
    model = _cred_string(
        creds.get("model_name") or creds.get("model") or os.environ.get("OPENAI_MODEL")
    ) or "qwen"
    if base and key:
        return base, key, model
    return None


def resolve_model():
    """Env wins. A Tanzu GenAI binding in VCAP_SERVICES is the fallback."""
    base = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/")
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "").strip() or "qwen"
    if base and key:
        return base, key, model
    raw = os.environ.get("VCAP_SERVICES", "")
    if not raw:
        return base, key, model
    try:
        services = json.loads(raw)
    except json.JSONDecodeError:
        return base, key, model
    for items in services.values():
        if not isinstance(items, list):
            continue
        for item in items:
            found = _from_credentials((item or {}).get("credentials"))
            if found:
                return found
    return base, key, model


def model_stream(message, history, tool_results):
    base, key, model = resolve_model()
    system = (
        "You are the Metal Oracle at the Metal Music Store. "
        "The tool results JSON is the only inventory and the only orders. "
        "Never invent an album, artist, price, order, or count. "
        "If asked how many albums, say the album_count integer first. "
        "Recommend only albums that appear in list_albums. "
        "If list_albums is empty, say there are no albums. "
        "Speak list_orders only when the question is about orders. "
        "If a tool returned an error, say that source is unavailable. "
        "Do not mention a grant. Do not say InsightOut."
    )
    facts = json.dumps(tool_results)
    messages = [{"role": "system", "content": system + "\nTool results:\n" + facts}]
    for turn in history or []:
        role = turn.get("role")
        content = turn.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message or ""})
    response = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages, "stream": True},
        stream=True,
        timeout=90,
    )
    if response.status_code >= 400:
        text = response.text[:300]
        yield sse({"error": f"private model HTTP {response.status_code}: {text}"})
        yield sse("[DONE]")
        return
    for line in response.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = (choices[0].get("delta") or {}).get("content") or ""
        if delta:
            yield sse({"delta": delta})
    yield sse("[DONE]")


def gather_tools(message):
    """Run the catalog tools. Orders are included when the question asks."""
    results = {}
    results["album_count"] = run_tool("album_count")
    results["list_albums"] = run_tool("list_albums")
    if wants_orders(message):
        try:
            results["list_orders"] = run_tool("list_orders")
        except Exception as exc:
            results["list_orders"] = {"error": str(exc)}
    return results


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "metal-oracle"})


@app.post("/mcp")
def mcp():
    body = request.get_json(silent=True) or {}
    method = body.get("method")
    req_id = body.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "metal-oracle", "version": "1.0.64"},
        }
    elif method == "tools/list":
        result = {"tools": TOOL_DEFS}
    elif method == "tools/call":
        params = body.get("params") or {}
        name = params.get("name")
        try:
            payload = run_tool(name)
            result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
        except Exception as exc:
            result = {
                "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                "isError": True,
            }
    else:
        return jsonify(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "method not found"}}
        )
    return jsonify({"jsonrpc": "2.0", "id": req_id, "result": result})


@app.post("/api/chat")
def chat():
    payload = request.get_json(silent=True) or {}
    message = payload.get("message") or ""
    history = payload.get("history") or []

    def generate():
        try:
            tool_results = gather_tools(message)
        except Exception as exc:
            yield sse({"error": f"catalog unavailable: {exc}"})
            yield sse("[DONE]")
            return
        count = (tool_results.get("album_count") or {}).get("count")
        if wants_count(message) and isinstance(count, int):
            noun = "album" if count == 1 else "albums"
            yield sse({"delta": f"The catalog has {count} {noun}. "})
        base, key, _model = resolve_model()
        if not base or not key:
            if wants_count(message) and isinstance(count, int):
                yield sse("[DONE]")
                return
            yield sse(
                {
                    "error": (
                        "The Metal Oracle needs the private language model. "
                        f"The catalog has {count} albums."
                    )
                }
            )
            yield sse("[DONE]")
            return
        try:
            yield from model_stream(message, history, tool_results)
        except requests.RequestException as exc:
            yield sse({"error": f"private model unreachable: {exc}"})
            yield sse("[DONE]")

    return Response(generate(), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005)
