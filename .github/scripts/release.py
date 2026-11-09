"""Check PR/release contracts and publish releases. Requires Python 3.11+."""

import argparse
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
SEMVER = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_version(text):
    value = tomllib.loads(text)["project"]["version"]
    require(
        isinstance(value, str) and re.fullmatch(SEMVER, value),
        "project.version must be bare X.Y.Z (no prerelease or build suffix)",
    )
    return value


def check_versions(root, version):
    # Each service owns its language/tooling. No root application is assumed.
    for directory in ("apps", "tools"):
        for project in sorted((root / directory).glob("*/pyproject.toml")):
            require(
                read_version(project.read_text(encoding="utf-8")) == version,
                f"{project.relative_to(root)} must match release version {version}",
            )
            data = tomllib.loads(project.read_text(encoding="utf-8"))["project"]
            lock = project.with_name("uv.lock")
            if lock.exists():
                packages = tomllib.loads(lock.read_text(encoding="utf-8"))["package"]
                own = [p for p in packages if p["name"] == data["name"]]
                require(
                    len(own) == 1 and own[0]["version"] == version,
                    f"{lock.relative_to(root)} project entry must match {version}",
                )
        for project in sorted((root / directory).glob("*/package.json")):
            data = json.loads(project.read_text(encoding="utf-8"))
            require(
                data.get("version") == version,
                f"{project.relative_to(root)} must match release version {version}",
            )
            lock = project.with_name("package-lock.json")
            if lock.exists():
                locked = json.loads(lock.read_text(encoding="utf-8"))
                require(
                    locked.get("version") == version
                    and locked.get("packages", {}).get("", {}).get("version")
                    == version,
                    f"{lock.relative_to(root)} root entries must match {version}",
                )


def release_version(branch):
    require(branch.startswith("release/v"), "Expected release/vX.Y.Z")
    value = branch.removeprefix("release/v")
    require(re.fullmatch(SEMVER, value), "Expected release/vX.Y.Z")
    return value


def check_pr(pr, version, repository):
    base, head = pr["base"]["ref"], pr["head"]["ref"]
    if base == "main":
        require(
            release_version(head) == version,
            "Release branch and package version differ",
        )
        require(
            pr["head"]["repo"]["full_name"].lower() == repository.lower(),
            "Release PR must come from this repository",
        )
        require(
            pr["title"] == f"Release v{version}",
            "Release PR title must be Release vX.Y.Z",
        )
        return True
    require(
        release_version(base) == version, "Target release and package version differ"
    )
    require(
        re.fullmatch(r"feature/[a-z0-9]+(?:[a-z0-9/-]*[a-z0-9])?", head),
        "Feature PR must use feature/<slug> and target its release branch",
    )
    return False


def image_matrix(root, repository):
    images = []
    names = set()
    for directory in ("apps", "services"):
        for dockerfile in sorted((root / directory).glob("*/Dockerfile")):
            name = dockerfile.parent.name
            require(
                re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name),
                f"Invalid service directory name: {name}",
            )
            require(name not in names, f"Duplicate service image name: {name}")
            names.add(name)
            images.append(
                {
                    "service": name,
                    "file": dockerfile.relative_to(root).as_posix(),
                    "image": f"ghcr.io/{repository.lower()}-{name}",
                }
            )
    return images


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def api(repository, path, *, method="GET", data=None, missing_ok=False):
    request = Request(
        f"https://api.github.com/repos/{repository}/{path}",
        data=json.dumps(data).encode() if data is not None else None,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "elice-techblog-rag-release",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        if missing_ok and exc.code == 404:
            return None
        raise ValueError(f"GitHub API {method} {path} failed: HTTP {exc.code}") from exc


def merged_release(prs, sha):
    matches = [
        pr
        for pr in prs
        if pr.get("merged_at")
        and pr.get("merge_commit_sha") == sha
        and pr["base"]["ref"] == "main"
    ]
    require(
        len(matches) == 1, "main commit must be the merge of exactly one release PR"
    )
    return matches[0]


def check_merge(pr, parents):
    require(
        len(parents) == 2 and parents[1] == pr["head"]["sha"],
        "Release must use a merge commit, not squash or rebase",
    )


def prepare_release(repository, sha):
    prs = api(repository, f"commits/{sha}/pulls?per_page=100")
    pr = merged_release(prs, sha)
    version = release_version(pr["head"]["ref"])
    check_versions(ROOT, version)
    check_pr(pr, version, repository)
    parents = git("show", "-s", "--format=%P", sha).split()
    check_merge(pr, parents)
    tag = f"v{version}"
    require(
        api(repository, f"git/ref/tags/{tag}", missing_ok=True) is None,
        f"Tag {tag} already exists; refusing to overwrite a release",
    )
    require(
        api(repository, f"releases/tags/{tag}", missing_ok=True) is None,
        f"Release {tag} already exists",
    )
    return pr, version


def outputs(version, images, **extra):
    values = {
        "version": version,
        "tag": f"v{version}",
        "matrix": json.dumps({"include": images}, separators=(",", ":")),
        "has_images": str(bool(images)).lower(),
        **extra,
    }
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.writelines(f"{key}={value}\n" for key, value in values.items())
    print(json.dumps(values, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "pr", "prepare", "publish"))
    parser.add_argument(
        "--release-version", help="Bare X.Y.Z for the read-only local check"
    )
    args = parser.parse_args()
    repository = os.environ.get("GITHUB_REPOSITORY", "Ootzk/elice-techblog-RAG")
    images = image_matrix(ROOT, repository)
    if args.command == "check":
        require(
            args.release_version is not None, "check requires --release-version X.Y.Z"
        )
        version = release_version("release/v" + args.release_version)
        check_versions(ROOT, version)
        outputs(version, images)
        return
    if args.command == "pr":
        event = json.loads(
            Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8")
        )
        pr = event["pull_request"]
        branch = pr["head"]["ref"] if pr["base"]["ref"] == "main" else pr["base"]["ref"]
        version = release_version(branch)
        check_versions(ROOT, version)
        is_release = check_pr(pr, version, repository)
        outputs(version, images, release_pr=str(is_release).lower())
        return
    sha = os.environ["GITHUB_SHA"]
    require(os.environ["GITHUB_REF"] == "refs/heads/main", "Release must run on main")
    require(
        git("rev-parse", "HEAD") == sha, "Checkout must match the triggering commit"
    )
    pr, version = prepare_release(repository, sha)
    if args.command == "prepare":
        outputs(version, images)
        return
    image_notes = "\n".join(f"- `{image['image']}:v{version}`" for image in images)
    body = (pr.get("body") or pr["title"]) + f"\n\nCommit: `{sha}`\n"
    body += (
        f"\n## Container images\n\n{image_notes}\n"
        if images
        else ("\nSource-only release: this version contains no service Dockerfiles.\n")
    )
    result = api(
        repository,
        "releases",
        method="POST",
        data={
            "tag_name": f"v{version}",
            "target_commitish": sha,
            "name": f"v{version}",
            "body": body,
            "draft": False,
            "prerelease": False,
        },
    )
    print(f"Created release: {result['html_url']}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Release check failed: {exc}") from exc
