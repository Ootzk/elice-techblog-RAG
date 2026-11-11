"""The dataset supplies facts; shared rubric snapshots supply ordered grading anchors."""

import pytest

from elice_knowledge.rubrics import criterion, load_rubric, validate_rubric


def test_shared_rubric_has_dimension_specific_anchors_and_is_independent_per_load():
    gold = load_rubric("gold")
    iron = load_rubric("iron")
    assert gold["scale"] == [0, 1, 2] and iron["scale"] == [0, 1]
    assert len(gold["dimensions"]) == 4 and len(iron["dimensions"]) == 1
    for name, definition in gold["dimensions"].items():
        assert set(definition["levels"]) == {"0", "1", "2"}
        text = criterion(gold, name)
        assert "한국어" in text and definition["description"] in text
    gold["dimensions"]["completeness"]["levels"]["2"] = "mutated"
    assert load_rubric("gold")["dimensions"]["completeness"]["levels"]["2"] != "mutated"


@pytest.mark.parametrize("mutation", ["kind", "scale", "anchors", "path"])
def test_invalid_rubric_cannot_silently_change_the_measurement_scale(mutation):
    rubric = load_rubric("gold")
    if mutation == "path":
        with pytest.raises(ValueError):
            load_rubric("gold", "../../secrets")
        return
    if mutation == "kind":
        rubric["kind"] = "iron"
    elif mutation == "scale":
        rubric["scale"] = [0, 1, 2, 3, 4]
    else:
        rubric["dimensions"]["correctness"]["levels"].pop("1")
    with pytest.raises(ValueError):
        validate_rubric(rubric, "gold")
