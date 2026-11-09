"""Inspect a frozen MLflow Dataset plan; enqueue paid work only with --submit."""

import argparse
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


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
        "--submit",
        action="store_true",
        help="Enqueue generation calls (may incur cost)",
    )
    args = parser.parse_args()
    if args.prompt and args.qa_profile_key:
        parser.error("Use either --qa-profile-key or --prompt")
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
        if args.prompt:
            selectors = dict(value.partition("=")[::2] for value in args.prompt)
            created = call("/qa-profiles", {"prompts": selectors})
            profile = created["key"]
            print(json.dumps({"qa_profile": created}, ensure_ascii=False, indent=2))
        else:
            profile = args.qa_profile_key or call("/qa-profiles")["active"]
        scorer = call("/scorer-profiles")["items"][0]["key"]
        plan = {
            "dataset_id": args.dataset_id,
            "dataset_digest": dataset["digest"],
            "qa_profile_key": profile,
            "scorer_profile_key": scorer,
            "record_ids": args.record_id,
            "history_mode": "provided",
        }
        output = (
            call("/evaluations", plan)
            if args.submit
            else {
                "mode": "preview",
                "record_count": len(args.record_id or dataset["records"]),
                "request": plan,
            }
        )
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except HTTPError as exc:
        raise SystemExit(f"API request failed: HTTP {exc.code}") from None
    except (URLError, ValueError, KeyError):
        raise SystemExit(
            "API connection or response failed; check server configuration"
        ) from None


if __name__ == "__main__":
    main()
