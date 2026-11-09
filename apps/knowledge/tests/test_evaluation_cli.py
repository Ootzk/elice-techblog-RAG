from test_evaluation_tools import load_tool


def test_wait_writes_terminal_results_and_paginates_without_generation():
    tool = load_tool("evaluate")
    calls = []

    def call(path):
        calls.append(path)
        if "/cases?" not in path:
            return {"status": "failed", "summary": {"failure_rate": 1.0}}
        return {"items": [{"record_id": str(i)} for i in range(50)] if "offset=0" in path else []}

    result = tool.wait_for_result(call, "id", 10)
    assert result["evaluation"]["status"] == "failed"
    assert len(result["cases"]) == 50
    assert calls[-1].endswith("offset=50")
