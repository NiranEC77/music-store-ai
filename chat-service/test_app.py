"""Local checks for the Metal Oracle. No database and no model."""

import json

import app as oracle


def events(response):
    body = response.get_data(as_text=True)
    out = []
    for block in body.split("\n\n"):
        line = block.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        out.append(data if data == "[DONE]" else json.loads(data))
    return out


def test_chat_does_not_call_store_tools(monkeypatch):
    def boom(_name):
        raise AssertionError("the agent called a store tool")

    monkeypatch.setattr(oracle, "run_tool", boom)
    monkeypatch.setattr(
        oracle,
        "run_turn",
        lambda message, history: oracle.TurnResult(text="The catalog door is shut."),
    )
    client = oracle.app.test_client()
    got = events(client.post("/api/chat", json={"message": "how many albums?"}))
    assert {"delta": "The catalog door is shut."} in got
    assert "[DONE]" in got


def test_tool_event_marks_a_denial(monkeypatch):
    monkeypatch.setattr(
        oracle,
        "run_turn",
        lambda message, history: oracle.TurnResult(
            text="AgentMinder denied list_orders.",
            tools=[{"name": "list_orders", "text": "policy denied tool 'list_orders'", "denied": True}],
        ),
    )
    client = oracle.app.test_client()
    got = events(client.post("/api/chat", json={"message": "show orders"}))
    assert {"tool": {"name": "list_orders", "denied": True, "text": "policy denied tool 'list_orders'"}} in got
    assert any(isinstance(item, dict) and "denied" in item.get("delta", "") for item in got)


def test_missing_model_does_not_read_the_catalog(monkeypatch):
    def boom(_name):
        raise AssertionError("catalog was read without AgentMinder")

    monkeypatch.setattr(oracle, "run_tool", boom)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("VCAP_SERVICES", raising=False)
    client = oracle.app.test_client()
    got = events(client.post("/api/chat", json={"message": "recommend thrash"}))
    assert any(isinstance(item, dict) and "private language model" in item.get("error", "") for item in got)
    assert not any("Pantera" in json.dumps(item) for item in got)


def test_mcp_list_and_call(monkeypatch):
    monkeypatch.setattr(oracle, "run_tool", lambda name: {"count": 6} if name == "album_count" else {})
    client = oracle.app.test_client()
    listed = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).get_json()
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert names == ["album_count", "list_albums", "list_orders"]
    called = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "album_count"}},
    ).get_json()
    assert json.loads(called["result"]["content"][0]["text"]) == {"count": 6}


def test_mcp_notification_is_accepted():
    client = oracle.app.test_client()
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert response.status_code == 202


def test_toolsets_point_at_the_gateway_only(monkeypatch):
    seen = []

    class FakeToolset:
        def __init__(self, url, **kwargs):
            seen.append({"url": url, "auth": kwargs.get("auth")})

    monkeypatch.setattr(oracle, "MCPToolset", FakeToolset)
    toolsets = oracle.build_toolsets({"https://agentminder.example/gw/one": "token-value"})
    assert len(toolsets) == 1
    assert seen[0]["url"] == "https://agentminder.example/gw/one"
    assert seen[0]["auth"] == "token-value"
    assert "mcp" not in seen[0]["url"].split("/")[-1]


def test_denial_text():
    assert oracle.is_denied("policy denied tool 'list_orders'")
    assert not oracle.is_denied('{"count": 6, "albums": [{"name": "Colony"}]}')


def test_vcap_binding_is_the_model(monkeypatch):
    monkeypatch.setenv(
        "VCAP_SERVICES",
        json.dumps({
            "ai-models": [{
                "credentials": {
                    "api_key": "secret-not-logged",
                    "api_base": "https://genai.example/v1",
                    "model_name": "qwen",
                }
            }]
        }),
    )
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert oracle.resolve_model() == ("https://genai.example/v1", "secret-not-logged", "qwen")
