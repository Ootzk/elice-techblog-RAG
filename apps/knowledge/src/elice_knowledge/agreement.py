"""Pairwise and panel reliability on the same frozen answers, by quality dimension."""

from collections import defaultdict
from itertools import combinations

from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_bundle import answer_digest, validate_bundle
from elice_knowledge.judges import DIMENSIONS, evaluation_row, inapplicable
from elice_knowledge.reliability import cohen, multirater, protocol, purpose
from elice_knowledge.rubrics import snapshot

STATUSES = {"scored", "not_applicable", "error", "pending"}


def review_packet(bundle, rater, rubric=None):
    validate_bundle(bundle)
    rubric = snapshot(bundle, rubric)
    records = {row["id"]: row for row in bundle["records"]}
    packet, labels = [], []
    for case in bundle["cases"]:
        if case["error"]:
            continue
        record = records[case["record_id"]]
        for dimension in DIMENSIONS[bundle["kind"]]:
            identity = dict(
                record_id=record["id"],
                answer_digest=answer_digest(record, case),
                dimension=dimension,
            )
            data = evaluation_row(record, case, dimension)
            packet.append(
                {
                    **identity,
                    "rubric": rubric["dimensions"][dimension],
                    "scale": rubric["scale"],
                    "scale_labels": rubric["scale_labels"],
                    "forced_na": inapplicable(record, case, dimension),
                    **{key: value for key, value in data.items() if key != "tags"},
                }
            )
            labels.append(
                {
                    **identity,
                    "rater": "human:" + rater,
                    "round": "1",
                    "score": None,
                    "status": "pending",
                    "protocol": digest(rubric),
                    "rationale": "",
                }
            )
    return packet, labels


def pairwise(left, right, scale=(0, 1, 2)):
    paired = [(left[key], right[key]) for key in sorted(left.keys() & right.keys())]
    applicable = [
        (a, b) for a, b in paired if type(a) is int and a in scale and type(b) is int and b in scale
    ]
    count = len(applicable)
    a_values, b_values = [a for a, _ in applicable], [b for _, b in applicable]
    kappa, undefined_reason = cohen(a_values, b_values, scale=scale)
    unweighted, _ = cohen(a_values, b_values, scale=scale, weights=None)
    quadratic, _ = cohen(a_values, b_values, scale=scale, weights="quadratic")
    statuses = [(a, b) for a, b in paired if a not in {"error", None} and b not in {"error", None}]
    return dict(
        paired_items=len(paired),
        unpaired_items=len(left.keys() ^ right.keys()),
        valid_pairs=count,
        excluded_pairs=len(paired) - count,
        agreement=sum(a == b for a, b in applicable) / count if count else None,
        mean_absolute_difference=sum(abs(a - b) for a, b in applicable) / count if count else None,
        cohen_kappa=kappa,
        cohen_kappa_unweighted=unweighted,
        cohen_kappa_quadratic=quadratic,
        confusion={
            f"{a}_{b}": sum(x == a and y == b for x, y in applicable) for a in scale for b in scale
        },
        kappa_undefined_reason=undefined_reason,
        applicability_pairs=len(statuses),
        applicability_agreement=(
            sum((a == "not_applicable") == (b == "not_applicable") for a, b in statuses)
            / len(statuses)
            if statuses
            else None
        ),
    )


def analyze(bundle, entries, rubric=None):
    validate_bundle(bundle)
    records = {row["id"]: row for row in bundle["records"]}
    cases = {row["record_id"]: row for row in bundle["cases"] if not row["error"]}
    rubric = snapshot(bundle, rubric)
    groups, seen = defaultdict(dict), set()
    for entry in entries:
        case_id = entry["record_id"]
        if (
            case_id not in cases
            or entry["dimension"] not in DIMENSIONS[bundle["kind"]]
            or entry["answer_digest"] != answer_digest(records[case_id], cases[case_id])
        ):
            raise ValueError("review_answer_binding_mismatch")
        if not isinstance(entry["rater"], str) or not entry["rater"].startswith(
            ("human:", "model:")
        ):
            raise ValueError("invalid_review_rater")
        from elice_knowledge.evaluation_schema import validate_rating

        validate_rating(entry, rubric["scale"], draft=True)
        label = (
            entry["score"]
            if entry["status"] == "scored"
            else (None if entry["status"] == "pending" else entry["status"])
        )
        identity = (
            entry["dimension"],
            entry["rater"],
            entry["protocol"],
            str(entry["round"]),
            case_id,
        )
        if identity in seen:
            raise ValueError("duplicate_review_label")
        seen.add(identity)
        groups[identity[:4]][case_id] = label
    comparisons, panels = [], []
    for dimension in DIMENSIONS[bundle["kind"]]:
        selected = [(key, labels) for key, labels in groups.items() if key[0] == dimension]
        selected.sort(key=lambda row: row[0])
        panels.extend(
            {"dimension": dimension, **row}
            for row in multirater(selected, sorted(cases), rubric["scale"])
        )
        for (a, left), (b, right) in combinations(selected, 2):
            comparison = (
                ("repeat_consistency" if a[2] == b[2] else "judge_revision")
                if a[1] == b[1]
                else "inter_rater"
            )
            comparisons.append(
                dict(
                    dimension=dimension,
                    left=dict(rater=a[1], round=a[3], profile_digest=a[2]),
                    right=dict(rater=b[1], round=b[3], profile_digest=b[2]),
                    comparison=comparison,
                    purpose=purpose(a[1], b[1], comparison),
                    left_protocol=a[2],
                    right_protocol=b[2],
                    **pairwise(left, right, rubric["scale"]),
                )
            )
    comparisons.sort(key=lambda row: row["purpose"] != "human_alignment")
    return dict(
        source_run_id=bundle["source_run_id"],
        source_digest=bundle["digest"],
        kind=bundle["kind"],
        qa_failed_cases=sum(bool(case["error"]) for case in bundle["cases"]),
        entries=len(entries),
        pending=sum(e["status"] == "pending" for e in entries),
        human_labels=sum(
            e["rater"].startswith("human:") and e["status"] in {"scored", "not_applicable"}
            for e in entries
        ),
        comparisons=comparisons,
        panels=panels,
        analysis_protocol=protocol(rubric["scale"]),
        rubric=rubric,
        rubric_digest=digest(rubric),
    )
