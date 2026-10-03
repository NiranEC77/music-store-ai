"""Metal Oracle. Port 5005.

The shop streams POST /api/chat here. A Pydantic AI agent answers.
When the model calls a tool, that call goes to an AgentMinder AI
Gateway URL. This process does not call the catalog, the order
service, or any MCP server for the agent.

POST /mcp is the catalog resource server AgentMinder calls after it
grants an intent. The agent never requests that URL.

Another MCP server is another AgentMinder gateway URL in
AGENTMINDER_GATEWAY_URLS. Register the server and its intents in
AgentMinder first. This file does not need a new tool function.
"""

import asyncio
import json
import os
from dataclasses import dataclass, field

import httpx2
import psycopg2
import psycopg2.extras
import requests
from flask import Flask, Response, jsonify, request
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

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
    "Album names, artists, prices, counts, and orders come only from tool results. "
    "Never invent an album, artist, price, order, or count. "
    "Tools are AgentMinder intents. If a tool result says the intent was denied "
    "or the policy denied the tool, say that AgentMinder denied it and name the tool. "
    "Do not fill in the missing fact. "
    "Use list_albums or album_count for catalog questions. "
    "Use list_orders only when that tool is available. "
    "If the question is about orders and list_orders is not available, say that "
    "AgentMinder denied the orders intent. Do not list albums as orders. "
    "If the question is not about this store's albums or orders, answer in one or two "
    "sentences and do not call a tool. "
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
    """Resource-server entry. AgentMinder calls POST /mcp. The agent does not."""
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


def am_tls_verify():
    return os.environ.get("AGENTMINDER_TLS_VERIFY", "true").lower() not in ("0", "false", "no")


def gateway_urls():
    raw = os.environ.get("AGENTMINDER_GATEWAY_URLS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def history_messages(history):
    out = []
    for turn in history or []:
        if not isinstance(turn, dict):
            continue
        content = turn.get("content") or ""
        if not content:
            continue
        if turn.get("role") == "user":
            out.append(ModelRequest(parts=[UserPromptPart(content=content)]))
        elif turn.get("role") == "assistant":
            out.append(ModelResponse(parts=[TextPart(content=content)]))
    return out


def tool_result_text(result):
    if isinstance(result, str):
        return result
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content", result)
    if isinstance(content, list):
        bits = []
        for block in content:
            if isinstance(block, str):
                bits.append(block)
            elif isinstance(block, dict):
                bits.append(str(block.get("text") if "text" in block else json.dumps(block)))
            else:
                text = getattr(block, "text", None)
                bits.append(str(text if text is not None else block))
        return "\n".join(bits)
    if isinstance(content, (dict, list)):
        return json.dumps(content)
    if content is not None:
        return str(content)
    return str(result)


def is_denied(text):
    low = (text or "").lower()
    return (
        "policy denied" in low
        or "intent was denied" in low
        or "backend unavailable" in low
        or "error 2002" in low
    )


async def record_tool_call(ctx, call_tool, name, tool_args):
    """Record the gateway result. A denial is text the model can say out loud."""
    try:
        result = await call_tool(name, tool_args)
    except Exception as exc:
        text = str(exc).strip() or type(exc).__name__
        if isinstance(ctx.deps, list):
            ctx.deps.append({"name": name, "text": text, "denied": True})
        return text
    text = tool_result_text(result)
    if isinstance(ctx.deps, list):
        ctx.deps.append({"name": name, "text": text, "denied": is_denied(text)})
    return result


def build_toolsets(tokens):
    """One Pydantic MCP toolset per AgentMinder gateway. Not the upstream MCP URL."""
    verify = am_tls_verify()
    toolsets = []
    for url, token in tokens.items():
        if not url or not token:
            continue
        toolsets.append(
            MCPToolset(
                url,
                auth=token,
                verify=verify,
                prefer_tasks=False,
                tool_error_behavior="error",
                process_tool_call=record_tool_call,
            )
        )
    return toolsets


async def mint_token(client, gateway_url):
    token_url = os.environ.get("AGENTMINDER_TOKEN_URL", "").strip()
    client_id = os.environ.get("AGENTMINDER_CLIENT_ID", "").strip()
    client_secret = os.environ.get("AGENTMINDER_CLIENT_SECRET", "").strip()
    if not token_url or not client_id or not client_secret:
        raise RuntimeError("AgentMinder client is not configured")
    response = await client.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "scope": "urn:iam:myscopes",
            "resource": gateway_url,
        },
        auth=(client_id, client_secret),
        headers={"Accept": "application/json"},
    )
    body = {}
    try:
        body = response.json()
    except Exception:
        body = {}
    token = body.get("access_token") if isinstance(body, dict) else ""
    if response.status_code >= 400 or not token:
        err = body.get("error") if isinstance(body, dict) else ""
        raise RuntimeError(f"AgentMinder token HTTP {response.status_code} {err}".strip())
    return token


async def mint_all(client):
    tokens = {}
    errors = []
    for url in gateway_urls():
        try:
            tokens[url] = await mint_token(client, url)
        except Exception as exc:
            errors.append(str(exc))
    return tokens, errors


@dataclass
class TurnResult:
    text: str = ""
    error: str = ""
    tools: list = field(default_factory=list)


async def oracle_turn(message, history):
    base, key, model_name = resolve_model()
    if not base or not key:
        return TurnResult(error="The Metal Oracle needs the private language model.")
    seen = []
    async with httpx2.AsyncClient(verify=am_tls_verify(), timeout=httpx2.Timeout(30.0)) as am_client:
        tokens, mint_errors = await mint_all(am_client)
    model_http = httpx2.AsyncClient(verify=tls_verify(), timeout=httpx2.Timeout(90.0))
    try:
        openai_client = AsyncOpenAI(
            base_url=base,
            api_key=key,
            http_client=model_http,
            max_retries=0,
        )
        model = OpenAIChatModel(model_name, provider=OpenAIProvider(openai_client=openai_client))
        agent = Agent(
            model,
            instructions=INSTRUCTIONS,
            deps_type=list,
            retries=1,
            toolsets=build_toolsets(tokens),
        )
        async with agent:
            result = await agent.run(
                message or "",
                message_history=history_messages(history),
                deps=seen,
            )
        text = result.output or ""
        if mint_errors and not tokens:
            seen.append({"name": "agentminder", "text": mint_errors[0], "denied": True})
        return TurnResult(text=text, tools=list(seen))
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        label = type(exc).__name__ if status is None else f"{type(exc).__name__} HTTP {status}"
        return TurnResult(error=f"private model: {label}", tools=list(seen))
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
    return jsonify(
        {
            "status": "ok",
            "service": "metal-oracle",
            "agent": "pydantic-ai",
            "doors": len(gateway_urls()),
        }
    )


@app.route("/mcp", methods=["GET", "POST", "DELETE"])
def mcp():
    """Catalog door for AgentMinder. The chat agent does not call this route."""
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
