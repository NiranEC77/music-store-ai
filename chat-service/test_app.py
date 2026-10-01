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


def test_count_comes_from_the_tool(monkeypatch):
    monkeypatch.setattr(oracle, "run_tool", lambda name: {
        "album_count": {"count": 1},
        "list_albums": {"count": 1, "albums": [{"name": "Colony", "artist": "In Flames"}]},
    }[name])
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = oracle.app.test_client()
    got = events(client.post("/api/chat", json={"message": "how many albums?"}))
    assert {"delta": "The catalog has 1 album. "} in got
    assert "[DONE]" in got
    assert not any(isinstance(item, dict) and "Pantera" in json.dumps(item) for item in got)


def test_catalog_failure_invents_nothing(monkeypatch):
    def boom(_name):
        raise OSError("connection refused")

    monkeypatch.setattr(oracle, "run_tool", boom)
    client = oracle.app.test_client()
    got = events(client.post("/api/chat", json={"message": "recommend thrash"}))
    assert any(isinstance(item, dict) and "catalog unavailable" in item.get("error", "") for item in got)
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
