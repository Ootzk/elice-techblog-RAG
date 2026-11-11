import pytest
from fastapi.testclient import TestClient

from elice_knowledge.main import create_app
from elice_knowledge.settings import Settings
from elice_knowledge.tools import TOOL_CONTRACTS, SearchOutput, ToolUnavailable

TOKEN = "t" * 24
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}


def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


INITIALIZE = rpc(
    "initialize",
    {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("elice_knowledge.mcp_server.active_index_key", lambda s: "c" * 64)
    with TestClient(create_app(Settings(mcp_api_token=TOKEN))) as test_client:
        yield test_client


def test_mcp_is_hidden_without_token_and_rejects_wrong_token(client):
    with TestClient(create_app(Settings())) as disabled:
        assert disabled.post("/mcp", json=INITIALIZE, headers=HEADERS).status_code == 404
    wrong = {**HEADERS, "Authorization": "Bearer " + "x" * 24}
    response = client.post("/mcp", json=INITIALIZE, headers=wrong)
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
    assert client.post("/mcp", json=INITIALIZE, headers=HEADERS).status_code == 200


def test_mcp_lists_the_agent_tool_contracts_as_read_only(client):
    tools = client.post("/mcp", json=rpc("tools/list"), headers=HEADERS).json()["result"]["tools"]
    assert [t["name"] for t in tools] == [c.name for c in TOOL_CONTRACTS]
    assert tools[0]["inputSchema"] == TOOL_CONTRACTS[0].input_schema()
    assert tools[0]["annotations"]["readOnlyHint"] is True


def test_mcp_tool_call_returns_structured_hits_and_safe_errors(client, monkeypatch):
    calls = []

    class Tools:
        def __init__(self, settings, index_key, client):
            self.index_key = index_key

        def invoke(self, name, arguments):
            calls.append((self.index_key, name, arguments))
            if arguments["query"] == "fail":
                raise ToolUnavailable(f"{name} is temporarily unavailable")
            return SearchOutput(
                tool=name, query=arguments["query"], index_key=self.index_key, hits=[]
            )

    monkeypatch.setattr("elice_knowledge.mcp_server.KnowledgeTools", Tools)
    params = {"name": "keyword_search", "arguments": {"query": "GPU"}}
    result = client.post("/mcp", json=rpc("tools/call", params), headers=HEADERS).json()["result"]
    assert result["structuredContent"]["tool"] == "keyword_search" and not result["isError"]
    assert calls == [("c" * 64, "keyword_search", {"query": "GPU"})]
    params["arguments"] = {"query": "fail"}
    failed = client.post("/mcp", json=rpc("tools/call", params), headers=HEADERS).json()["result"]
    assert failed["isError"] and "temporarily unavailable" in failed["content"][0]["text"]


def test_mcp_reports_invalid_arguments_without_running_search(client):
    params = {"name": "meaning_search", "arguments": {"query": " "}}
    result = client.post("/mcp", json=rpc("tools/call", params), headers=HEADERS).json()["result"]
    assert result["isError"] and "query" in result["content"][0]["text"]
