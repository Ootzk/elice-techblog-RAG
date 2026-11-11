"""Versioned shared ordinal anchors for model and human grading."""

import json
import re
from copy import deepcopy
from pathlib import Path

from elice_knowledge.corpus import digest

CONTRACT = "rubric-v1"
DEFAULTS = {"gold": "gold-ordinal-v1", "iron": "iron-binary-v1"}
DIMENSIONS = {
    "gold": ("correctness", "completeness", "groundedness", "relevance"),
    "iron": ("behavior",),
    "online": ("groundedness", "relevance"),
}


def validate_rubric(rubric, kind):
    expected_scale = [0, 1, 2] if kind == "gold" else [0, 1]
    if (
        kind not in DEFAULTS
        or rubric.get("kind") != kind
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,80}", rubric.get("id", ""))
        or rubric.get("scale") != expected_scale
        or set(rubric.get("dimensions", {})) != set(DIMENSIONS[kind])
        or not isinstance(rubric.get("instructions"), str)
        or not rubric["instructions"].strip()
        or set(rubric.get("scale_labels", {})) != {str(s) for s in expected_scale}
        or any(
            not isinstance(value, str) or not value.strip()
            for value in rubric.get("scale_labels", {}).values()
        )
    ):
        raise ValueError("invalid_rubric")
    for definition in rubric["dimensions"].values():
        if (
            set(definition.get("levels", {})) != {str(s) for s in expected_scale}
            or any(
                not isinstance(definition.get(key), str) or not definition[key].strip()
                for key in ("title", "description", "not_applicable")
            )
            or any(not isinstance(v, str) or not v.strip() for v in definition["levels"].values())
        ):
            raise ValueError("invalid_rubric_anchors")
    return rubric


def load_rubric(kind, rubric_id=None):
    identity = rubric_id or DEFAULTS[kind]
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,80}", identity):
        raise ValueError("invalid_rubric_id")
    path = Path(__file__).parent / "data" / "rubrics" / (identity + ".json")
    rubric = json.loads(path.read_text())
    if rubric["id"] != identity:
        raise ValueError("rubric_id_mismatch")
    return validate_rubric(rubric, kind)


def snapshot(bundle, rubric=None):
    return deepcopy(
        validate_rubric(rubric, bundle["kind"])
        if rubric is not None
        else load_rubric(bundle["kind"])
    )


def rubric_digest(rubric):
    return digest(rubric)


def criterion(rubric, dimension):
    definition = rubric["dimensions"][dimension]
    return "\n".join(
        [
            rubric["instructions"],
            definition["title"],
            definition["description"],
            *[f"{score}: {definition['levels'][str(score)]}" for score in rubric["scale"]],
            "not_applicable: " + definition["not_applicable"],
            "위 등급 또는 not_applicable과 함께 짧은 한국어 판정 사유를 반환하세요.",
        ]
    )
