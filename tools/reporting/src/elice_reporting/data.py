"""Load frozen artifacts and normalize tables without evaluating models or statistics."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import pandas as pd


@dataclass
class ReportData:
    kind: str
    report: dict
    inputs: dict
    source: str
    run_id: str | None = None
    links: dict = field(default_factory=dict)


def find_root(start=None):
    start = Path(start or Path.cwd()).resolve()
    for directory in (start, *start.parents):
        if (directory / "AGENTS.md").is_file() and (directory / "apps").is_dir():
            return directory
    raise ValueError("Run the notebook within the project or supply the repository path")


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _validate_gradings(gradings):
    for grading in gradings:
        if grading.get("schema") != "evaluation-v2":
            raise ValueError("Unsupported grading schema")
        if (
            grading.get("contract") != "rubric-v1"
            or grading.get("measurement_contract") != "ragas-diagnostics-v1"
        ):
            raise ValueError("Only rubric-v1 grading results are supported")


def _validate(kind, report, inputs, source, run_id=None):
    if kind not in {"analysis", "comparison"} or report.get("schema") != "evaluation-v2":
        raise ValueError("Unsupported report schema or kind")
    if kind == "comparison":
        if report.get("comparison_contract") != "comparison-v1":
            raise ValueError("Unsupported comparison contract")
        if not {"baseline", "candidate"} <= inputs.keys():
            raise ValueError("Comparison requires both input snapshots")
        if report.get("digest") != _digest({k: v for k, v in report.items() if k != "digest"}):
            raise ValueError("Comparison content digest mismatch")
        for side in ("baseline", "candidate"):
            if report[side]["input_digest"] != _digest(inputs[side]):
                raise ValueError("Comparison input digest mismatch")
            _validate_gradings(inputs[side]["gradings"])
    else:
        bundle = inputs.get("bundle", {})
        if bundle.get("digest") != _digest({k: v for k, v in bundle.items() if k != "digest"}):
            raise ValueError("Analysis bundle digest mismatch")
        if report.get("source_run_id") != bundle.get("source_run_id") or report.get(
            "source_digest"
        ) != bundle.get("digest"):
            raise ValueError("Analysis input binding mismatch")
        _validate_gradings(inputs["gradings"])
        rubric_digest = report.get("rubric_digest")
        if rubric_digest != _digest(report.get("rubric")) or any(
            g.get("rubric_digest") != rubric_digest or _digest(g.get("rubric")) != rubric_digest
            for g in inputs["gradings"]
        ):
            raise ValueError("Analysis requires the same rubric snapshot")
        expected = {g["rater_profile_digest"] for g in inputs["gradings"]}
        for key, profile in inputs.get("profiles", {}).items():
            if key not in expected or _digest(profile) != key:
                raise ValueError("Analysis grading profile binding mismatch")
            if profile.get("rubric_digest") != rubric_digest:
                raise ValueError("Analysis grading rubric binding mismatch")
    return ReportData(kind, report, inputs, source, run_id)


def from_files(kind, report_path, inputs_path):
    """Analysis inputs contain bundle+gradings; comparison uses comparison-inputs.json."""
    return _validate(
        kind,
        json.loads(Path(report_path).read_text()),
        json.loads(Path(inputs_path).read_text()),
        "local",
    )


def demo(kind):
    if kind not in {"analysis", "comparison"}:
        raise ValueError("Unknown demo kind")
    content = json.loads(files("elice_reporting").joinpath("examples", kind + ".json").read_text())
    return _validate(kind, content["report"], content["inputs"], "demo")


def _run_link(uri, run, identity):
    parts = urlsplit(uri)
    experiment = getattr(run.info, "experiment_id", None)
    url = None
    if parts.scheme in {"http", "https"} and parts.netloc and experiment is not None:
        base = urlunsplit(
            (parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path.rstrip("/"), "", "")
        )
        url = (
            f"{base}/#/experiments/{quote(str(experiment), safe='')}"
            f"/runs/{quote(identity, safe='')}"
        )
    return {"run_id": identity, "url": url}


def from_mlflow(kind, run_id, cache_dir, *, tracking_uri=None):
    """Read-only MLflow access. Does not create runs, upload artifacts or invoke models."""
    import mlflow
    from mlflow.exceptions import MlflowException

    tracking = mlflow.MlflowClient(tracking_uri=tracking_uri)
    uri = tracking_uri or mlflow.get_tracking_uri()
    links = {}

    def read(identity, filename):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
            raise ValueError("Invalid run ID")
        directory = Path(cache_dir) / identity
        directory.mkdir(parents=True, exist_ok=True)
        path = tracking.download_artifacts(identity, filename, str(directory))
        return json.loads(Path(path).read_text())

    run = tracking.get_run(run_id)
    if (
        run.info.status != "FINISHED"
        or run.data.tags.get("evaluation.kind") != kind
        or run.data.tags.get("evaluation.schema") != "evaluation-v2"
    ):
        raise ValueError("Select a finished compatible analysis/comparison run")
    if kind == "analysis":
        report = read(run_id, "agreement.json")
        original = tracking.get_run(report["source_run_id"])
        if original.data.tags.get("evaluation.kind") != "answers":
            raise ValueError("Invalid analysis source run")
        links = {"answers": _run_link(uri, original, report["source_run_id"]), "gradings": []}
        inputs = {
            "bundle": read(report["source_run_id"], "evaluation-bundle.json"),
            "gradings": read(run_id, "grading-inputs.json")["results"],
        }
        if run.data.tags.get("source_bundle_digest") != inputs["bundle"]["digest"]:
            raise ValueError("Analysis source digest mismatch")
        profiles = {}
        expected = {g["rater_profile_digest"] for g in inputs["gradings"]}
        for grading_id in report.get("grading_run_ids", []):
            grade = tracking.get_run(grading_id)
            profile = read(grading_id, "rater-profile.json")
            key = _digest(profile)
            if (
                grade.data.tags.get("evaluation.kind") != "grading"
                or grade.data.tags.get("source_run_id") != report["source_run_id"]
                or grade.data.tags.get("source_bundle_digest") != inputs["bundle"]["digest"]
                or grade.data.tags.get("rater_profile_digest") != key
                or key not in expected
            ):
                raise ValueError("Analysis grading profile binding mismatch")
            profiles[key] = profile
            links["gradings"].append(
                {
                    **_run_link(uri, grade, grading_id),
                    "rater": grade.data.tags.get("rater_id"),
                    "round": grade.data.tags.get("round"),
                    "profile_digest": key,
                    "synthetic": grade.data.tags.get("review.provenance") == "synthetic",
                }
            )
            if profile.get("type") == "model":
                observations = []
                # These are historical observations, never the Gateway's current config.
                traces = tracking.search_traces(
                    locations=[grade.info.experiment_id],
                    filter_string=f"attributes.run_id = '{grading_id}'",
                    max_results=3,
                )
                for item in traces:
                    try:
                        trace = tracking.get_trace(item.info.trace_id)
                    except MlflowException:
                        observations.append(
                            {"trace_id": item.info.trace_id, "models": [], "status": "unavailable"}
                        )
                        continue
                    models = sorted(
                        {
                            span.attributes["mlflow.llm.model"]
                            for span in trace.data.spans
                            if isinstance(span.attributes.get("mlflow.llm.model"), str)
                        }
                    )
                    observations.append({"trace_id": item.info.trace_id, "models": models})
                links["gradings"][-1]["model_observations"] = observations
        if profiles:
            inputs["profiles"] = profiles
    elif kind == "comparison":
        report = read(run_id, "comparison-result.json")
        inputs = read(run_id, "comparison-inputs.json")
        if run.data.tags.get("comparison_digest") != report.get("digest"):
            raise ValueError("Comparison digest mismatch")
    else:
        raise ValueError("Unknown report kind")
    data = _validate(kind, report, inputs, "mlflow", run_id)
    data.links = links
    return data


def _identity(subject):
    return f"{subject['rater']} · {subject['round']}회차"


def _cases(bundle):
    records = {r["id"]: r for r in bundle["records"]}
    return pd.DataFrame(
        [
            dict(
                record_id=c["record_id"],
                question=records[c["record_id"]]["inputs"]["question"],
                answer=(c.get("result") or {}).get("answer"),
                error=c.get("error"),
            )
            for c in bundle["cases"]
        ]
    )


def analysis_frames(data):
    if data.kind != "analysis":
        raise ValueError("Expected analysis data")
    pairs = [
        dict(
            dimension=r["dimension"],
            purpose=r["purpose"],
            left=_identity(r["left"]),
            right=_identity(r["right"]),
            pair=_identity(r["left"]) + " ↔ " + _identity(r["right"]),
            cohen_kappa=r["cohen_kappa"],
            agreement=r["agreement"],
            valid_pairs=r["valid_pairs"],
            excluded_pairs=r["excluded_pairs"],
            undefined_reason=r["kappa_undefined_reason"],
        )
        for r in data.report["comparisons"]
    ]
    panels = [
        {k: v for k, v in r.items() if k not in {"members", "rater_coverage"}}
        for r in data.report["panels"]
    ]
    labels = [
        dict(
            rater=g["rater_id"],
            round=g["round"],
            **{k: v for k, v in label.items() if k not in {"rater", "round"}},
        )
        for g in data.inputs["gradings"]
        for label in g["ratings"]
    ]
    return dict(
        pairs=pd.DataFrame(
            pairs,
            columns=[
                "dimension",
                "purpose",
                "left",
                "right",
                "pair",
                "cohen_kappa",
                "agreement",
                "valid_pairs",
                "excluded_pairs",
                "undefined_reason",
            ],
        ),
        panels=pd.DataFrame(panels),
        labels=pd.DataFrame(labels),
        cases=_cases(data.inputs["bundle"]),
    )


def comparison_frames(data):
    if data.kind != "comparison":
        raise ValueError("Expected comparison data")

    def summary(rows):
        return pd.DataFrame(
            [
                {
                    **{
                        k: v
                        for k, v in r.items()
                        if k not in {"cases", "counts", "confidence_interval"}
                    },
                    **r["counts"],
                    "ci_low": r["confidence_interval"][0] if r["confidence_interval"] else None,
                    "ci_high": r["confidence_interval"][1] if r["confidence_interval"] else None,
                }
                for r in rows
            ]
        )

    transitions = [
        dict(metric=row["metric"], rater=row.get("rater", "QA resources"), **case)
        for row in data.report["quality"] + data.report["resources"]
        for case in row["cases"]
    ]
    pairs = [
        dict(
            dimension=r["dimension"],
            purpose=r["purpose"],
            raters=" ↔ ".join(r["raters"]),
            baseline_kappa=r["baseline"]["cohen_kappa"],
            candidate_kappa=r["candidate"]["cohen_kappa"],
            delta_kappa=r["delta"]["cohen_kappa"],
            paired_cases=len(r["paired_record_ids"]),
        )
        for r in data.report["reliability"]["pairs"]
    ]
    return dict(
        quality=summary(data.report["quality"]),
        resources=summary(data.report["resources"]),
        transitions=pd.DataFrame(transitions),
        pairs=pd.DataFrame(pairs),
        conditions=pd.DataFrame(data.report["configuration_changes"]),
        baseline_cases=_cases(data.inputs["baseline"]["bundle"]),
        candidate_cases=_cases(data.inputs["candidate"]["bundle"]),
    )
