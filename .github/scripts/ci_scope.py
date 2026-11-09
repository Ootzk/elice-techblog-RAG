"""Select PR checks from the merge-base diff without installing dependencies."""

import argparse
import json
import os
import subprocess


def select_checks(paths, *, release_pr=False):
    web = knowledge = release_pr
    for path in paths:
        if (
            path.startswith(
                (
                    "docs/",
                    "corpus/",
                    ".vscode/",
                    "tools/sql/",
                    "tools/evaluation/gold_set/",
                    "tools/evaluation/iron_set/",
                )
            )
            or path.endswith(".md")
            or path in {".gitignore", ".gitattributes", ".editorconfig", "LICENSE"}
        ):
            continue
        elif path.startswith("apps/web/"):
            web = True
        elif path.startswith(("apps/knowledge/", "tools/evaluation/")):
            knowledge = True
        else:
            # Shared configuration, CI and unclassified paths affect both apps.
            web = knowledge = True
    return {"web": str(web).lower(), "knowledge": str(knowledge).lower()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--release-pr", choices=("true", "false"), required=True)
    args = parser.parse_args()
    changed = subprocess.check_output(
        [
            "git",
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            f"{args.base}...{args.head}",
            "--",
        ]
    )
    paths = changed.decode("utf-8").split("\0")
    checks = select_checks(filter(None, paths), release_pr=args.release_pr == "true")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.writelines(f"{key}={value}\n" for key, value in checks.items())
    print(json.dumps(checks))


if __name__ == "__main__":
    main()
