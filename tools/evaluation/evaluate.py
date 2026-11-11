"""Inspect a frozen MLflow Dataset plan; enqueue paid work only with --submit."""

import argparse
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def wait_for_result(call, evaluation_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = call("/evaluations/" + evaluation_id)
        if result["status"] in {"completed", "failed", "interrupted"}:
            cases = []
            while True:
                page = call(
                    f"/evaluations/{evaluation_id}/cases?limit=50&offset={len(cases)}"
                )["items"]
                cases.extend(page)
                if len(page) < 50:
                    break
            return {"evaluation": result, "cases": cases}
        time.sleep(2)
    raise SystemExit(f"Evaluation wait timed out; job remains active: {evaluation_id}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--qa-profile-key")
    parser.add_argument(
        "--prompt",
        action="append",
        metavar="NAME=VERSION|ALIAS",
        help="Create an experiment QA profile pinned to these registry prompts",
    )
    parser.add_argument("--record-id", action="append")
    parser.add_argument(
        "--generation-options",
        type=json.loads,
        default=None,
        help="JSON object of generation request options",
    )
    parser.add_argument(
        "--embedding-options",
        type=json.loads,
        default=None,
        help="JSON object of embedding request options",
    )
    parser.add_argument(
        "--judge-options",
        type=json.loads,
        default=None,
        help="JSON object mapping judge names to request options",
    )
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Enqueue generation calls (may incur cost)",
    )
    args = parser.parse_args()
    if args.qa_profile_key and (
        args.prompt
        or args.generation_options is not None
        or args.embedding_options is not None
    ):
        parser.error("Use either --qa-profile-key or prompt/generation options")
    if (args.wait or args.report) and not args.submit:
        parser.error("--wait/--report requires --submit")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.report and args.report.exists():
        parser.error("Report already exists; choose a new path")
    token = os.environ.get("EVALUATION_API_TOKEN")
    if not token:
        parser.error("Set EVALUATION_API_TOKEN in the environment")

    def call(path, body=None):
        request = Request(
            args.base_url.rstrip("/") + "/api" + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=60) as response:
            return json.load(response)

    try:
        dataset = call("/evaluation-datasets/" + quote(args.dataset_id, safe=""))
        if (
            args.prompt
            or args.generation_options is not None
            or args.embedding_options is not None
        ):
            selectors = dict(value.partition("=")[::2] for value in (args.prompt or []))
            created = call(
                "/qa-profiles",
                {
                    "prompts": selectors,
                    "generation_options": args.generation_options or {},
                    "embedding_options": args.embedding_options or {},
                },
            )
            profile = created["key"]
            print(json.dumps({"qa_profile": created}, ensure_ascii=False, indent=2))
        else:
            profile = args.qa_profile_key or call("/qa-profiles")["active"]
        scoring = call("/scorer-profiles")
        scorer = scoring["items"][0]["key"]
        plan = {
            "dataset_id": args.dataset_id,
            "dataset_digest": dataset["digest"],
            "qa_profile_key": profile,
            "scorer_profile_key": scorer,
            "record_ids": args.record_id,
            "history_mode": "provided",
            "judge_options": args.judge_options or {},
        }
        output = (
            call("/evaluations", plan)
            if args.submit
            else {
                "mode": "preview",
                "record_count": len(args.record_id or dataset["records"]),
                "judges_enabled": scoring["judges_enabled"],
                "dataset_kind": dataset["kind"],
                "request": plan,
            }
        )
        print(json.dumps(output, ensure_ascii=False, indent=2))
        if args.wait or args.report:
            report = wait_for_result(call, output["id"], args.timeout)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n"
                )
            print(json.dumps(report["evaluation"], ensure_ascii=False, indent=2))
            if report["evaluation"]["status"] != "completed":
                raise SystemExit(
                    "Evaluation failed/interrupted; inspect the saved report"
                )
    except HTTPError as exc:
        raise SystemExit(f"API request failed: HTTP {exc.code}") from None
    except (URLError, ValueError, KeyError):
        raise SystemExit(
            "API connection or response failed; check server configuration"
        ) from None


if __name__ == "__main__":
    main()
