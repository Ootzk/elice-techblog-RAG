"""Library-backed ordinal agreement with explicit cohorts and missing-value policy."""

import math
from collections import Counter
from importlib.metadata import version


def protocol(scale=(0, 1, 2)):
    return {
        "name": "agreement-ordinal-v1",
        "cohen": "sklearn.metrics.cohen_kappa_score",
        "fleiss": "statsmodels.stats.inter_rater.fleiss_kappa(method=fleiss)",
        "krippendorff": "krippendorff.alpha(level_of_measurement=ordinal)",
        "versions": {
            p: version(p) for p in ("ragas", "scikit-learn", "statsmodels", "krippendorff")
        },
        "measurement_level": "ordinal",
        "fleiss_measurement_level": "nominal",
        "scale": list(scale),
        "cohen_weights": "linear",
        "minimum_cases": 2,
        "minimum_panel_subjects": 3,
        "minimum_alpha_ratings_per_case": 2,
        "panel_policy": "one_round_and_profile_per_subject",
        "fleiss_cases": "all_selected_subjects_have_scores",
        "alpha_cases": "at_least_two_selected_subjects_have_scores",
        "excluded_statuses": ["not_applicable", "error", "pending_or_missing"],
        "primary_comparison": "human_model_cohen_kappa",
    }


def cohen(left, right, *, scale=(0, 1, 2), weights="linear"):
    if len(left) != len(right) or len(left) < 2:
        return None, "insufficient_pairs"
    if len(set(left) | set(right)) < 2:
        return None, "constant_identical_labels"
    from sklearn.metrics import cohen_kappa_score

    value = float(cohen_kappa_score(left, right, labels=list(scale), weights=weights))
    return (value, None) if math.isfinite(value) else (None, "non_finite_result")


def purpose(left, right, comparison):
    if comparison != "inter_rater":
        return comparison
    types = {r.split(":", 1)[0] for r in (left, right)}
    if "baseline" in types:
        return "baseline_comparison"
    return (
        "human_alignment"
        if types == {"human", "model"}
        else "human_reliability"
        if types == {"human"}
        else "model_agreement"
    )


def panel(selected, case_ids, *, scope, scale=(0, 1, 2)):
    """Rows are selected grading rounds; columns are frozen answer records."""
    import numpy as np

    members = [dict(rater=key[1], profile_digest=key[2], round=key[3]) for key, _ in selected]
    counts = Counter(m["rater"] for m in members)
    matrix = np.array(
        [
            [
                labels.get(key)
                if type(labels.get(key)) is int and labels.get(key) in scale
                else np.nan
                for key in case_ids
            ]
            for _, labels in selected
        ],
        dtype=float,
    ).reshape(len(selected), len(case_ids))
    present = np.isfinite(matrix)
    per_case = present.sum(axis=0)
    complete = per_case == len(selected) if selected else np.zeros(len(case_ids), dtype=bool)
    pairable = per_case >= 2
    result = {
        "scope": scope,
        "role": "supplemental",
        "members": members,
        "subjects": len(counts),
        "selected_gradings": len(selected),
        "total_cases": len(case_ids),
        "complete_cases": int(complete.sum()),
        "pairable_cases": int(pairable.sum()),
        "fleiss_excluded_cases": int((~complete).sum()),
        "alpha_excluded_cases": int((~pairable).sum()),
        "valid_ratings": int(present.sum()),
        "alpha_effective_subjects": int(present[:, pairable].any(axis=1).sum()),
        "rater_coverage": [
            {
                **member,
                "valid_ratings": int(present[i].sum()),
                "alpha_valid_ratings": int(present[i, pairable].sum()),
            }
            for i, member in enumerate(members)
        ],
        "not_applicable_ratings": sum(
            labels.get(k) == "not_applicable" for _, labels in selected for k in case_ids
        ),
        "error_ratings": sum(labels.get(k) == "error" for _, labels in selected for k in case_ids),
        "missing_ratings": sum(labels.get(k) is None for _, labels in selected for k in case_ids),
        "fleiss_kappa": None,
        "krippendorff_alpha": None,
        "fleiss_undefined_reason": None,
        "alpha_undefined_reason": None,
    }
    ambiguous = sorted(rater for rater, n in counts.items() if n > 1)
    result["ambiguous_subjects"] = ambiguous
    reason = (
        "multiple_gradings_per_subject"
        if ambiguous
        else "insufficient_raters"
        if len(counts) < 3
        else None
    )
    if reason:
        result.update(fleiss_undefined_reason=reason, alpha_undefined_reason=reason)
        return result
    # Fleiss uses the complete fixed panel; alpha retains partially scored cases.
    for field, reason_field, usable in (
        ("fleiss_kappa", "fleiss_undefined_reason", complete),
        ("krippendorff_alpha", "alpha_undefined_reason", pairable),
    ):
        ratings = matrix[:, usable]
        if int(usable.sum()) < 2:
            result[reason_field] = "insufficient_cases"
            continue
        if field == "krippendorff_alpha" and result["alpha_effective_subjects"] < 3:
            result[reason_field] = "insufficient_effective_raters"
            continue
        if np.unique(ratings[np.isfinite(ratings)]).size < 2:
            result[reason_field] = "constant_labels"
            continue
        if field == "fleiss_kappa":
            from statsmodels.stats.inter_rater import fleiss_kappa

            table = np.column_stack([(ratings == i).sum(axis=0) for i in scale])
            value = float(fleiss_kappa(table, method="fleiss"))
        else:
            import krippendorff

            value = float(
                krippendorff.alpha(
                    reliability_data=ratings,
                    value_domain=list(scale),
                    level_of_measurement="ordinal",
                )
            )
        if math.isfinite(value):
            result[field] = value
        else:
            result[reason_field] = "non_finite_result"
    return result


def multirater(selected, case_ids, scale=(0, 1, 2)):
    """Report the selected panel and homogeneous subsets without pooling repetitions."""
    selected = [row for row in selected if not row[0][1].startswith("baseline:")]
    cohorts = [("all", selected)]
    humans = [row for row in selected if row[0][1].startswith("human:")]
    models = [row for row in selected if row[0][1].startswith("model:")]
    if humans and models:
        for name, rows in (("humans_only", humans), ("models_only", models)):
            if len({key[1] for key, _ in rows}) >= 3:
                cohorts.append((name, rows))
    return [panel(rows, case_ids, scope=name, scale=scale) for name, rows in cohorts]


def report_metrics(report):
    """Only finite numbers become MLflow metrics; unavailable coefficients stay in JSON."""
    from elice_knowledge.corpus import digest

    metrics = {}
    for row in report["comparisons"]:
        key = digest([row["left"], row["right"]])[:12]
        prefix = f"agreement.{row['dimension']}.{key}."
        for name in (
            "agreement",
            "cohen_kappa",
            "valid_pairs",
            "cohen_kappa_unweighted",
            "cohen_kappa_quadratic",
            "mean_absolute_difference",
            "excluded_pairs",
            "paired_items",
            "unpaired_items",
            "applicability_pairs",
            "applicability_agreement",
        ):
            if row[name] is not None:
                metrics[prefix + name] = row[name]
    for row in report["panels"]:
        key = digest(row["members"])[:12]
        prefix = f"agreement.panel.{row['dimension']}.{row['scope']}.{key}."
        for name in (
            "fleiss_kappa",
            "krippendorff_alpha",
            "subjects",
            "total_cases",
            "complete_cases",
            "pairable_cases",
            "fleiss_excluded_cases",
            "alpha_excluded_cases",
            "alpha_effective_subjects",
            "valid_ratings",
            "not_applicable_ratings",
            "error_ratings",
            "missing_ratings",
        ):
            if row[name] is not None:
                metrics[prefix + name] = row[name]
    return {k: v for k, v in metrics.items() if math.isfinite(v)}
