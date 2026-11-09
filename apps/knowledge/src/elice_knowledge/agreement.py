"""Pairwise binary agreement on the same frozen answers, by quality dimension."""

from collections import defaultdict
from itertools import combinations

from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_bundle import answer_digest, validate_bundle
from elice_knowledge.judges import DIMENSIONS, evaluation_row, instructions

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
                    "rubric": instructions(dimension),
                    **{key: value for key, value in data.items() if key != "tags"},
                }
            )
            labels.append(
                {
                    **identity,
                    "rater": "human:" + rater,
                    "round": "1",
                    "label": None,
                    "protocol": digest(instructions(dimension)),
                    "rationale": "",
                }
            )
    return packet, labels


def machine_labels(bundle, judgments, round_id):
    records = {row["id"]: row for row in bundle["records"]}
    cases = {row["record_id"]: row for row in bundle["cases"]}
    return [
        dict(
            record_id=row["record_id"],
            answer_digest=answer_digest(records[row["record_id"]], cases[row["record_id"]]),
            dimension=cell["dimension"],
            rater=cell["rater"],
            round=round_id,
            label=cell["status"],
            rationale=cell.get("rationale"),
            protocol=digest({"name": name, "version": cell["version"]}),
        )
        for row in judgments
        for name, cell in row["judges"].items()
    ]


def pairwise(left, right):
    paired = [(left[key], right[key]) for key in left.keys() & right.keys()]
    applicable = [(a, b) for a, b in paired if a in {"pass", "fail"} and b in {"pass", "fail"}]
    count = len(applicable)
    confusion = {
        f"{a}_{b}": sum(x == a and y == b for x, y in applicable)
        for a in ("pass", "fail")
        for b in ("pass", "fail")
    }
    observed = sum(a == b for a, b in applicable) / count if count else None
    expected = (
        sum(
            (sum(a == label for a, _ in applicable) / count)
            * (sum(b == label for _, b in applicable) / count)
            for label in ("pass", "fail")
        )
        if count
        else None
    )
    kappa = (observed - expected) / (1 - expected) if count >= 2 and expected < 1 else None
    scored_statuses = [
        (a, b) for a, b in paired if a in LABELS - {"error"} and b in LABELS - {"error"}
    ]
    return dict(
        paired_items=len(paired),
        binary_pairs=count,
        excluded_pairs=len(paired) - count,
        agreement=observed,
        cohen_kappa=kappa,
        confusion=confusion,
        kappa_undefined_reason=(
            "insufficient_pairs"
            if count < 2
            else "constant_identical_labels"
            if expected == 1
            else None
        ),
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
    groups, seen, protocols = defaultdict(dict), set(), {}
    for entry in entries:
        case_id = entry["record_id"]
        if (
            case_id not in cases
            or entry["dimension"] not in DIMENSIONS[bundle["kind"]]
            or entry["answer_digest"] != answer_digest(records[case_id], cases[case_id])
        ):
            raise ValueError("review_answer_binding_mismatch")
        label = entry["label"]
        if label is not None and label not in LABELS:
            raise ValueError("invalid_review_label")
        identity = (entry["dimension"], entry["rater"], str(entry["round"]), case_id)
        if identity in seen:
            raise ValueError("duplicate_review_label")
        seen.add(identity)
        protocol = entry["protocol"]
        if identity[:3] in protocols and protocols[identity[:3]] != protocol:
            raise ValueError("mixed_judge_protocols_in_round")
        protocols[identity[:3]] = protocol
        groups[identity[:3]][case_id] = label
    comparisons = []
    for dimension in DIMENSIONS[bundle["kind"]]:
        selected = [(key, labels) for key, labels in groups.items() if key[0] == dimension]
        for (a, left), (b, right) in combinations(selected, 2):
            comparisons.append(
                dict(
                    dimension=dimension,
                    left=dict(rater=a[1], round=a[2]),
                    right=dict(rater=b[1], round=b[2]),
                    comparison=(
                        "repeat_consistency" if protocols[a] == protocols[b] else "judge_revision"
                    )
                    if a[1] == b[1]
                    else "inter_rater",
                    left_protocol=protocols[a],
                    right_protocol=protocols[b],
                    **pairwise(left, right),
                )
            )
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
    )
