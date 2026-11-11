"""Copy and execute a report template from one MLflow run."""

import argparse
from pathlib import Path

import nbformat
from nbclient import NotebookClient

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("agreement", "repeatability", "comparison"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    notebook_path = output.with_suffix(".ipynb")
    if output.exists() or notebook_path.exists():
        raise SystemExit("출력 파일이 이미 있습니다. 다른 경로를 선택하세요.")
    if output.suffix.lower() != ".html":
        raise SystemExit("출력 파일은 .html이어야 합니다.")
    output.parent.mkdir(parents=True, exist_ok=True)
    template = ROOT / "tools/reporting/notebooks/templates" / f"{args.kind}.ipynb"
    notebook = nbformat.read(template, as_version=4)
    source = notebook.cells[2].source
    source = source.replace('RUN_ID = ""', f"RUN_ID = {args.run_id!r}", 1)
    lines = source.splitlines()
    lines = [
        f"OUTPUT_HTML = Path({str(output)!r})"
        if line.startswith("OUTPUT_HTML = ")
        else line
        for line in lines
    ]
    notebook.cells[2].source = "from pathlib import Path\n" + "\n".join(lines)
    nbformat.write(notebook, notebook_path)
    NotebookClient(
        notebook,
        timeout=300,
        kernel_name="python3",
        resources={"metadata": {"path": str(ROOT)}},
    ).execute()
    if not output.is_file():
        raise SystemExit("보고서 파일을 만들지 못했습니다.")
    print(f"보고서: {output}\n노트북: {notebook_path}")


if __name__ == "__main__":
    main()
