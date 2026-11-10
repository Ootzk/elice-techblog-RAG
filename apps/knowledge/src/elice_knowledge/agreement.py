"""Pairwise and panel reliability on the same frozen answers, by quality dimension."""

from collections import defaultdict
from itertools import combinations

from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_bundle import answer_digest, validate_bundle
from elice_knowledge.judges import DIMENSIONS, evaluation_row
from elice_knowledge.reliability import cohen, multirater, protocol, purpose

LABELS = {"pass", "fail", "not_applicable", "error"}


def review_packet(bundle, rater):
    validate_bundle(bundle)
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
                    "rubric": bundle["rubrics"][dimension],
                    **{key: value for key, value in data.items() if key != "tags"},
                }
            )
            labels.append(
                {
                    **identity,
                    "rater": "human:" + rater,
                    "round": "1",
                    "label": None,
                    "protocol": digest(bundle["rubrics"][dimension]),
                    "rationale": "",
                }
            )
    return packet, labels


def pairwise(left, right):
    paired = [(left[key], right[key]) for key in sorted(left.keys() & right.keys())]
    applicable = [(a, b) for a, b in paired if a in {"pass", "fail"} and b in {"pass", "fail"}]
    count = len(applicable)
    confusion = {
        f"{a}_{b}": sum(x == a and y == b for x, y in applicable)
        for a in ("pass", "fail")
        for b in ("pass", "fail")
    }
    observed = sum(a == b for a, b in applicable) / count if count else None
    kappa, undefined_reason = cohen([a for a, _ in applicable], [b for _, b in applicable])
    scored_statuses = [
        (a, b) for a, b in paired if a in LABELS - {"error"} and b in LABELS - {"error"}
    ]
    return dict(
        paired_items=len(paired),
        unpaired_items=len(left.keys() ^ right.keys()),
        binary_pairs=count,
        excluded_pairs=len(paired) - count,
        agreement=observed,
        cohen_kappa=kappa,
        confusion=confusion,
        kappa_undefined_reason=undefined_reason,
        applicability_pairs=len(scored_statuses),
        applicability_agreement=(
            sum((a == "not_applicable") == (b == "not_applicable") for a, b in scored_statuses)
            / len(scored_statuses)
            if scored_statuses
            else None
        ),
    )


def analyze(bundle, entries):
    validate_bundle(bundle)
    records = {row["id"]: row for row in bundle["records"]}
    cases = {row["record_id"]: row for row in bundle["cases"] if not row["error"]}
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
        label = entry["label"]
        if label is not None and label not in LABELS:
            raise ValueError("invalid_review_label")
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
            {"dimension": dimension, **row} for row in multirater(selected, sorted(cases))
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
                    **pairwise(left, right),
                )
            )
    comparisons.sort(key=lambda row: row["purpose"] != "human_alignment")
    return dict(
        source_run_id=bundle["source_run_id"],
        source_digest=bundle["digest"],
        kind=bundle["kind"],
        qa_failed_cases=sum(bool(case["error"]) for case in bundle["cases"]),
        entries=len(entries),
        pending=sum(e["label"] is None for e in entries),
        human_labels=sum(
            e["rater"].startswith("human:") and e["label"] in LABELS - {"error"} for e in entries
        ),
        comparisons=comparisons,
        panels=panels,
        analysis_protocol=protocol(),
    )
