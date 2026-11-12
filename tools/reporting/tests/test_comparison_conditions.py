from types import SimpleNamespace

from elice_reporting.repeated_comparison_sections import conditions


def test_comparison_summary_contains_only_changed_settings():
    def side(model, top_k):
        return {
            "inputs": {
                "agreements": [
                    {
                        "bundle": {
                            "kind": "gold",
                            "records": [],
                            "qa_profile": {"generation_model": model, "top_k": top_k},
                        },
                        "agreement": {"rubric": {"id": "gold-ordinal-v1"}},
                    }
                ],
            },
        }

    data = SimpleNamespace(
        inputs={"baseline": side("gpt-6.1-terra", 1), "candidate": side("gpt-6.1-terra", 5)},
        report={
            "baseline": {"answers_runs": 3},
            "candidate": {"answers_runs": 3},
            "grading_counts": {},
        },
        links={},
    )
    view = conditions(data, {})
    summary = view[1].object
    assert "검색당 결과 수" in summary
    assert "<td>1</td><td>5</td>" in summary
    assert "답변 모델" not in summary

    data.inputs["candidate"]["inputs"]["agreements"][0]["bundle"]["qa_profile"][
        "generation_model"
    ] = "qwen"
    summary = conditions(data, {})[1].object
    assert "검색당 결과 수" in summary and "답변 모델" in summary
