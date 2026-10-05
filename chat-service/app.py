"""Metal Oracle. Port 5005.

The shop streams POST /api/chat here. This is a normal chat against
the private model. It is not an agent and it does not call a gateway.

The reply is grounded in the catalog and recent orders this process
reads itself. POST /mcp still exposes those reads for a later MCP
registration. The chat route does not call /mcp.
"""

import asyncio
import json
import os
from dataclasses import dataclass, field

import httpx
import psycopg2
import psycopg2.extras
import requests
from flask import Flask, Response, jsonify, request
from openai import AsyncOpenAI

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

INSTRUCTIONS = (
    "You are the Metal Oracle at the Metal Music Store. "
    "Album names, artists, prices, counts, and orders come only from the "
    "catalog block in this prompt. Never invent an album, artist, price, order, or count. "
    "If the catalog or the orders say they are unavailable, say that and do not guess. "
    "If the question is not about this store, answer in one or two sentences. "
    "Do not say InsightOut."
)


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
    """Resource-server entry for POST /mcp. The chat route does not call this."""
    if name not in TOOLS:
        raise KeyError(name)
    return TOOLS[name]()


def sse(obj):
    if obj == "[DONE]":
        return b"data: [DONE]\n\n"
    return f"data: {json.dumps(obj)}\n\n".encode()


def _cred_string(value):
    return value.strip() if isinstance(value, str) else ""


def _from_credentials(creds):
    if not isinstance(creds, dict):
        return None
    nested = creds.get("endpoint") if isinstance(creds.get("endpoint"), dict) else {}
    key = _cred_string(
        creds.get("api_key") or nested.get("api_key") or creds.get("apiKey") or nested.get("apiKey")
    )
    base = (
        nested.get("openai_api_base")
        or creds.get("openai_api_base")
        or creds.get("api_base")
        or nested.get("api_base")
        or creds.get("url")
        or nested.get("url")
    )
    base = _cred_string(base).rstrip("/")
    if base.endswith("/openai"):
        base = base + "/v1"
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


def tls_verify():
    return os.environ.get("OPENAI_TLS_VERIFY", "true").lower() not in ("0", "false", "no")


def catalog_context():
    """Facts the model may use. A failed read stays unavailable."""
    parts = []
    try:
        parts.append("Catalog: " + json.dumps(tool_list_albums()))
    except Exception as exc:
        parts.append(f"Catalog: unavailable ({type(exc).__name__}).")
    try:
        parts.append("Orders: " + json.dumps(tool_list_orders()))
    except Exception as exc:
        parts.append(f"Orders: unavailable ({type(exc).__name__}).")
    return "\n".join(parts)


def chat_messages(message, history):
    messages = [{"role": "system", "content": INSTRUCTIONS + "\n\n" + catalog_context()}]
    for turn in history or []:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = turn.get("content") or ""
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message or ""})
    return messages


@dataclass
class TurnResult:
    text: str = ""
    error: str = ""
    tools: list = field(default_factory=list)


async def oracle_turn(message, history):
    base, key, model_name = resolve_model()
    if not base or not key:
        return TurnResult(error="The Metal Oracle needs the private language model.")
    model_http = httpx.AsyncClient(verify=tls_verify(), timeout=httpx.Timeout(90.0))
    try:
        client = AsyncOpenAI(
            base_url=base,
            api_key=key,
            http_client=model_http,
            max_retries=0,
        )
        result = await client.chat.completions.create(
            model=model_name,
            messages=chat_messages(message, history),
        )
        text = ""
        if result.choices:
            text = result.choices[0].message.content or ""
        return TurnResult(text=text)
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        label = type(exc).__name__ if status is None else f"{type(exc).__name__} HTTP {status}"
        return TurnResult(error=f"private model: {label}")
    finally:
        await model_http.aclose()


def run_turn(message, history):
    return asyncio.run(oracle_turn(message, history))


def _rpc_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id, message, code=-32000):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_rpc(msg):
    method = msg.get("method")
    req_id = msg.get("id")
    if method == "initialize":
        return _rpc_result(
            req_id,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "music-catalog", "version": "1"},
            },
        )
    if method in ("notifications/initialized", "initialized") or req_id is None:
        return None
    if method == "ping":
        return _rpc_result(req_id, {})
    if method == "tools/list":
        return _rpc_result(req_id, {"tools": TOOL_DEFS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        try:
            payload = run_tool(name)
            result = {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}
        except Exception as exc:
            result = {
                "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                "isError": True,
            }
        return _rpc_result(req_id, result)
    return _rpc_error(req_id, f"unknown method {method}", -32601)


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "metal-oracle"})


@app.route("/mcp", methods=["GET", "POST", "DELETE"])
def mcp():
    """Catalog door. The chat route does not call this."""
    if request.method == "GET":
        return Response(
            '{"error":"POST JSON-RPC to /mcp"}',
            status=405,
            mimetype="application/json",
        )
    if request.method == "DELETE":
        return Response(status=204)
    body = request.get_json(silent=True) or {}
    if isinstance(body, list):
        replies = [handle_rpc(item) for item in body if isinstance(item, dict)]
        replies = [item for item in replies if item is not None]
        return jsonify(replies)
    reply = handle_rpc(body)
    if reply is None:
        return Response(status=202)
    return jsonify(reply)


@app.post("/api/chat")
def chat():
    payload = request.get_json(silent=True) or {}
    message = payload.get("message") or ""
    history = payload.get("history") or []

    def generate():
        try:
            turn = run_turn(message, history)
        except Exception as exc:
            yield sse({"error": f"oracle failed: {type(exc).__name__}"})
            yield sse("[DONE]")
            return
        for tool in turn.tools:
            yield sse(
                {
                    "tool": {
                        "name": tool.get("name"),
                        "denied": bool(tool.get("denied")),
                        "text": tool.get("text") or "",
                    }
                }
            )
        if turn.text:
            yield sse({"delta": turn.text})
        elif turn.error:
            yield sse({"error": turn.error})
        else:
            yield sse({"error": "The Metal Oracle returned an empty answer."})
        yield sse("[DONE]")

    return Response(generate(), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005)
