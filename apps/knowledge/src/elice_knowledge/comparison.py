"""A paired comparison of frozen A/B agreement inputs; never invokes models."""

import math
import re
from importlib.metadata import version
from itertools import combinations
from numbers import Real

from elice_knowledge.agreement import build_agreement, pairwise
from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_bundle import validate_bundle
from elice_knowledge.evaluation_schema import SCHEMA, validate_grading
from elice_knowledge.judges import DIMENSIONS
from elice_knowledge.reliability import multirater
from elice_knowledge.reliability import purpose as comparison_purpose

SCORES = {0, 1, 2}


def validate_snapshot(snapshot):
    bundle, agreement, gradings = snapshot["bundle"], snapshot["agreement"], snapshot["gradings"]
    validate_bundle(bundle)
    if (
        agreement.get("schema") != SCHEMA
        or agreement.get("source_run_id") != bundle["source_run_id"]
    ):
        raise ValueError("comparison_agreement_source_mismatch")
    if not gradings:
        raise ValueError("comparison_requires_gradings")
    for key, value in snapshot.get("profiles", {}).items():
        if digest(value) != key:
            raise ValueError("comparison_profile_digest_mismatch")
    subjects = {}
    for grading in gradings:
        validate_grading(bundle, grading)
        if not grading["complete"]:
            raise ValueError("comparison_requires_complete_grading")
        if grading["rater_id"].startswith("baseline:"):
            continue
        if grading["rater_id"] in subjects:
            raise ValueError("comparison_requires_one_grading_per_subject")
        subjects[grading["rater_id"]] = grading
    if len({g["rubric_digest"] for g in gradings}) != 1:
        raise ValueError("agreement_requires_same_rubric")
    recomputed = build_agreement(
        bundle, [r for g in gradings for r in g["ratings"]], gradings[0]["rubric"]
    )
    if any(agreement.get(key) != value for key, value in recomputed.items()):
        raise ValueError("comparison_agreement_inputs_or_protocol_mismatch")
    if not subjects:
        raise ValueError("comparison_requires_gradings")
    return subjects


def changes(left, right, path="qa_profile"):
    if left == right:
        return []
    if isinstance(left, dict) and isinstance(right, dict):
        return [
            row
            for key in sorted(left.keys() | right.keys())
            for row in changes(left.get(key), right.get(key), path + "." + key)
        ]

    def compact(value):
        return (
            dict(digest=digest(value), characters=len(str(value)))
            if len(str(value)) > 512
            else value
        )

    return [dict(path=path, baseline=compact(left), candidate=compact(right))]


def number(value):
    return (
        float(value)
        if not isinstance(value, bool) and isinstance(value, Real) and math.isfinite(value)
        else None
    )


def scalar(
    metric,
    left,
    right,
    ids,
    *,
    higher_is_better=True,
    neutral=False,
    seed=42,
    resamples=2000,
    confidence=0.95,
):
    import numpy as np
    from scipy.stats import bootstrap

    cases, a, b = [], [], []
    counts = dict(improved=0, regressed=0, tie=0)
    if neutral:
        counts = dict(increased=0, decreased=0, tie=0)
    for key in ids:
        x, y = left.get(key), right.get(key)
        before, after = number(x), number(y)
        if before is None or after is None:
            cases.append(
                dict(
                    record_id=key,
                    baseline=before,
                    candidate=after,
                    delta=None,
                    change="excluded",
                    baseline_reason=None
                    if before is not None
                    else (x if isinstance(x, str) else "missing_or_invalid"),
                    candidate_reason=None
                    if after is not None
                    else (y if isinstance(y, str) else "missing_or_invalid"),
                )
            )
            continue
        delta = after - before
        change = (
            "tie"
            if abs(delta) <= 1e-12
            else (
                ("increased" if delta > 0 else "decreased")
                if neutral
                else ("improved" if (delta > 0) == higher_is_better else "regressed")
            )
        )
        counts[change] += 1
        cases.append(
            dict(record_id=key, baseline=before, candidate=after, delta=delta, change=change)
        )
        a.append(before)
        b.append(after)
    interval, unavailable = None, "disabled" if not resamples else "insufficient_paired_cases"
    if len(a) >= 2 and resamples:
        result = bootstrap(
            (np.array(a), np.array(b)),
            lambda x, y, axis: np.mean(y - x, axis=axis),
            paired=True,
            vectorized=True,
            method="percentile",
            n_resamples=resamples,
            confidence_level=confidence,
            rng=np.random.default_rng(seed),
            batch=200,
        )
        interval = [float(result.confidence_interval.low), float(result.confidence_interval.high)]
        unavailable = None
    return dict(
        metric=metric,
        higher_is_better=None if neutral else higher_is_better,
        interpretation="score_shift" if neutral else "paired_outcome",
        paired_cases=len(a),
        excluded_cases=len(ids) - len(a),
        baseline_mean=sum(a) / len(a) if a else None,
        candidate_mean=sum(b) / len(b) if b else None,
        delta_mean=sum(y - x for x, y in zip(a, b, strict=True)) / len(a) if a else None,
        confidence_interval=interval,
        interval_unavailable_reason=unavailable,
        counts=counts,
        cases=cases,
    )


def labels(grading, dimension):
    return {
        r["record_id"]: r["score"] if r["status"] == "scored" else r["status"]
        for r in grading["ratings"]
        if r["dimension"] == dimension
    }


def quality_values(bundle, grading, dimension, *, raw):
    source_cases = {c["record_id"]: c for c in bundle["cases"]}
    if raw:
        values = {
            r["record_id"]: r["score"] if r["status"] == "scored" else r["status"]
            for r in grading["measurements"]
            if r["metric"] == dimension
        }
    else:
        values = {key: label for key, label in labels(grading, dimension).items()}
    return {
        key: "generation_error" if case["error"] else values.get(key, "missing")
        for key, case in source_cases.items()
    }


def resource_values(bundle, name):
    usage = dict(
        input_tokens="prompt_tokens",
        cached_input_tokens="cached_tokens",
        output_tokens="completion_tokens",
        total_tokens="total_tokens",
    )
    result = {}
    for case in bundle["cases"]:
        if name == "failure_rate":
            value = int(bool(case["error"]))
        elif case["error"]:
            value = "generation_error"
        elif name == "qa_duration_ms":
            value = case.get("duration_ms", "missing")
        elif name in usage:
            value = (case.get("result") or {}).get("usage", {}).get(usage[name], "missing")
        else:
            value = (case.get("result") or {}).get(name, "missing")
        result[case["record_id"]] = value
    return result


def matched_reliability(left, right, ids, kind):
    """Recompute coefficients with the same observed rater/question support on both sides."""
    pairs, panels, support = [], [], []
    scale = (0, 1, 2) if kind == "gold" else (0, 1)
    raters = sorted(left)
    for dimension in DIMENSIONS[kind]:
        la = {r: labels(left[r], dimension) for r in raters}
        lb = {r: labels(right[r], dimension) for r in raters}
        support.extend(
            dict(
                dimension=dimension,
                rater=r,
                record_id=k,
                baseline=la[r].get(k, "missing"),
                candidate=lb[r].get(k, "missing"),
                included=la[r].get(k) in SCORES and lb[r].get(k) in SCORES,
            )
            for r in raters
            for k in ids
        )
        for first, second in combinations(raters, 2):
            eligible = [
                k
                for k in ids
                if all(
                    values.get(k) in SCORES
                    for values in (la[first], la[second], lb[first], lb[second])
                )
            ]
            a = pairwise(
                {k: la[first][k] for k in eligible},
                {k: la[second][k] for k in eligible},
                scale=scale,
            )
            b = pairwise(
                {k: lb[first][k] for k in eligible},
                {k: lb[second][k] for k in eligible},
                scale=scale,
            )
            # Applicability agreement on an all-scored mask would be tautologically 1.
            for stats in (a, b):
                stats.pop("applicability_pairs")
                stats.pop("applicability_agreement")
            pairs.append(
                dict(
                    dimension=dimension,
                    raters=[first, second],
                    purpose=comparison_purpose(first, second, "inter_rater"),
                    paired_record_ids=eligible,
                    excluded_record_ids=sorted(set(ids) - set(eligible)),
                    baseline=a,
                    candidate=b,
                    delta={
                        m: b[m] - a[m] if a[m] is not None and b[m] is not None else None
                        for m in ("agreement", "cohen_kappa")
                    },
                )
            )
        selected = []
        for ratings, other, grading in ((la, lb, left), (lb, la, right)):
            selected.append(
                [
                    (
                        (
                            dimension,
                            r,
                            grading[r]["rater_profile_digest"],
                            str(grading[r]["round"]),
                        ),
                        {
                            k: ratings[r][k]
                            if ratings[r].get(k) in SCORES and other[r].get(k) in SCORES
                            else None
                            for k in ids
                        },
                    )
                    for r in raters
                ]
            )
        a = {p["scope"]: p for p in multirater(selected[0], ids, scale)}
        b = {p["scope"]: p for p in multirater(selected[1], ids, scale)}
        for scope in a:
            panels.append(
                dict(
                    dimension=dimension,
                    scope=scope,
                    baseline=a[scope],
                    candidate=b[scope],
                    delta={
                        m: b[scope][m] - a[scope][m]
                        if a[scope][m] is not None and b[scope][m] is not None
                        else None
                        for m in ("fleiss_kappa", "krippendorff_alpha")
                    },
                )
            )
    pairs.sort(key=lambda p: p["purpose"] != "human_alignment")
    return dict(
        pairs=pairs,
        panels=panels,
        support=support,
        policy="intersection_of_scored_rater_question_observations",
    )


def compare(baseline, candidate, *, purpose, hypothesis, seed=42, resamples=2000, confidence=0.95):
    if purpose not in {"system", "judge"} or not hypothesis.strip():
        raise ValueError("comparison_purpose_and_hypothesis_required")
    if (
        not 0 <= seed <= 2**32 - 1
        or not (resamples == 0 or 100 <= resamples <= 100000)
        or not 0 < confidence < 1
    ):
        raise ValueError("invalid_comparison_bootstrap_configuration")
    if baseline["agreement_run_id"] == candidate["agreement_run_id"]:
        raise ValueError("comparison_requires_distinct_agreement_runs")
    left, right = validate_snapshot(baseline), validate_snapshot(candidate)
    a, b = baseline["bundle"], candidate["bundle"]
    if any(a[k] != b[k] for k in ("dataset_digest", "kind")):
        raise ValueError("comparison_dataset_or_rubric_mismatch")
    if baseline["agreement"]["rubric_digest"] != candidate["agreement"]["rubric_digest"]:
        raise ValueError("comparison_rubric_mismatch")
    if set(left) != set(right):
        raise ValueError("comparison_rater_panel_mismatch")
    for rater in left:
        if (purpose == "system" or rater.startswith("human:")) and left[rater][
            "rater_profile_digest"
        ] != right[rater]["rater_profile_digest"]:
            raise ValueError("comparison_rater_profile_mismatch")
        if (
            purpose == "judge"
            and rater.startswith("human:")
            and {
                (v["record_id"], v["dimension"]): (v["status"], v["score"])
                for v in left[rater]["ratings"]
            }
            != {
                (v["record_id"], v["dimension"]): (v["status"], v["score"])
                for v in right[rater]["ratings"]
            }
        ):
            raise ValueError("comparison_human_reference_changed")
    if purpose == "system" and a["source_run_id"] == b["source_run_id"]:
        raise ValueError("system_comparison_requires_distinct_answers")
    if purpose == "judge" and a["digest"] != b["digest"]:
        raise ValueError("judge_comparison_requires_same_frozen_answers")
    ra, rb = ({r["id"]: r for r in source["records"]} for source in (a, b))
    ids = sorted(ra.keys() & rb.keys())
    if not ids:
        raise ValueError("comparison_no_shared_records")
    if any(ra[k] != rb[k] for k in ids):
        raise ValueError("comparison_question_or_expectation_changed")
    options = dict(seed=seed, resamples=resamples, confidence=confidence)
    quality = []
    for rater in sorted(left):
        for dimension in DIMENSIONS[a["kind"]]:
            quality.append(
                dict(
                    rater=rater,
                    **scalar(
                        "rubric." + dimension + ".mean",
                        quality_values(a, left[rater], dimension, raw=False),
                        quality_values(b, right[rater], dimension, raw=False),
                        ids,
                        neutral=purpose == "judge",
                        **options,
                    ),
                )
            )
        if left[rater]["rater_type"] == "model":
            names = sorted({m["metric"] for m in left[rater]["measurements"]})
            for name in names:
                quality.append(
                    dict(
                        rater=rater,
                        **scalar(
                            "ragas." + name + ".mean",
                            quality_values(a, left[rater], name, raw=True),
                            quality_values(b, right[rater], name, raw=True),
                            ids,
                            neutral=purpose == "judge",
                            **options,
                        ),
                    )
                )
    resources = [
        scalar(
            name,
            resource_values(a, name),
            resource_values(b, name),
            ids,
            higher_is_better=False,
            neutral=name == "cached_input_tokens",
            **options,
        )
        for name in (
            "failure_rate",
            "qa_duration_ms",
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "total_tokens",
            "llm_call_count",
            "tool_call_count",
        )
    ]
    result = dict(
        schema=SCHEMA,
        comparison_contract="comparison-v1",
        purpose=purpose,
        hypothesis=hypothesis,
        baseline={k: baseline[k] for k in ("agreement_run_id",)},
        candidate={k: candidate[k] for k in ("agreement_run_id",)},
        protocol=dict(
            seed=seed,
            resamples=resamples,
            confidence=confidence,
            bootstrap="paired_percentile_mean_delta",
            resampling_unit="dataset_record_id",
            coefficient_intervals=False,
            resource_scope="answer_generation_only",
            repeated_trials=False,
            versions={
                p: version(p)
                for p in ("numpy", "scipy", "ragas", "scikit-learn", "statsmodels", "krippendorff")
            },
        ),
        coverage=dict(
            shared_record_ids=ids,
            baseline_only=sorted(ra.keys() - rb.keys()),
            candidate_only=sorted(rb.keys() - ra.keys()),
        ),
        configuration_changes=changes(a["qa_profile"], b["qa_profile"]),
        grading_profile_changes=[
            dict(
                rater=r,
                baseline_digest=left[r]["rater_profile_digest"],
                candidate_digest=right[r]["rater_profile_digest"],
                changes=changes(
                    baseline["profiles"][left[r]["rater_profile_digest"]],
                    candidate["profiles"][right[r]["rater_profile_digest"]],
                    path="rater_profile",
                )
                if left[r]["rater_profile_digest"] in baseline.get("profiles", {})
                and right[r]["rater_profile_digest"] in candidate.get("profiles", {})
                else None,
            )
            for r in sorted(left)
            if left[r]["rater_profile_digest"] != right[r]["rater_profile_digest"]
        ],
        quality=quality,
        resources=resources,
        reliability=matched_reliability(left, right, ids, a["kind"]),
    )
    for label, snapshot in (("baseline", baseline), ("candidate", candidate)):
        result[label].update(
            source_run_id=snapshot["bundle"]["source_run_id"],
            source_bundle_digest=snapshot["bundle"]["digest"],
            input_digest=digest(snapshot),
            gradings=[
                {k: g[k] for k in ("rater_id", "rater_profile_digest", "round")}
                for g in snapshot["gradings"]
            ],
        )
    result["digest"] = digest(result)
    return result


def comparison_metrics(report):
    values = {
        "comparison.coverage.shared_cases": len(report["coverage"]["shared_record_ids"]),
        "comparison.coverage.baseline_only_cases": len(report["coverage"]["baseline_only"]),
        "comparison.coverage.candidate_only_cases": len(report["coverage"]["candidate_only"]),
    }
    for row in report["quality"] + report["resources"]:
        if row.get("rater"):
            rater = re.sub(r"[^a-zA-Z0-9_.-]", "_", row["rater"])[:70]
            prefix = f"comparison.{rater}_{digest(row['rater'])[:8]}.{row['metric']}."
        else:
            prefix = "comparison.resources." + row["metric"] + "."
        for field in (
            "baseline_mean",
            "candidate_mean",
            "delta_mean",
            "paired_cases",
            "excluded_cases",
        ):
            if row[field] is not None:
                values[prefix + field] = row[field]
        for field, count in row["counts"].items():
            values[prefix + field + "_cases"] = count
        if row["confidence_interval"] is not None:
            values[prefix + "ci_low"], values[prefix + "ci_high"] = row["confidence_interval"]
    for group in ("pairs", "panels"):
        for row in report["reliability"][group]:
            identity = row.get("scope") or digest(row["raters"])[:12]
            prefix = f"comparison.reliability.{group}.{row['dimension']}.{identity}."
            for field, value in row["delta"].items():
                if value is not None:
                    values[prefix + "delta_" + field] = value
    return values
