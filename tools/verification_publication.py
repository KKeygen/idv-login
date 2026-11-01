"""Generate and accumulate release and hotfix verification records."""
import argparse
import json
import os
from pathlib import Path
import py_compile
import re
import subprocess
import sys
import tempfile
import time

from generate_verification import (
    load_existing_records, marshal_sha256_from_pyc, record_key, source_sha256,
)


CATALOG = "ext/verification.json"
CONFIG = "assets/cloudRes.json"


def git(*args, cwd=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)


def write_records(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(records, key=lambda r: (
        r["module"], r["source_sha256"], r.get("marshal_sha256", ""))),
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def hotfix_pairs(config):
    result = set()
    for item in config.get("hotfix", []):
        module, commit = item.get("target_module"), item.get("target_commit")
        if not module or not commit:
            raise ValueError("Each hotfix must specify target_module and target_commit")
        module = module.replace("/", ".")
        if not all(part.isidentifier() for part in module.split(".")):
            raise ValueError(f"Invalid hotfix module: {module!r}")
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
            raise ValueError(f"Hotfix target must be a pinned commit: {commit!r}")
        result.add((module, commit.lower()))
    return result


def plan_hotfix(before, after, output):
    previous = {} if set(before) == {"0"} else json.loads(
        git("show", f"{before}:{CONFIG}").stdout)
    current = json.loads(git("show", f"{after}:{CONFIG}").stdout)
    added = sorted(hotfix_pairs(current) - hotfix_pairs(previous))
    Path(output).write_text(json.dumps([
        {"target_module": module, "target_commit": commit} for module, commit in added
    ], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"changed={'true' if added else 'false'}\n")
    print(f"New hotfix module/commit pairs: {len(added)}")


def generate_hotfix(plan, output):
    if sys.implementation.name != "cpython" or sys.version_info[:3] != (3, 12, 10) or sys.flags.optimize:
        raise RuntimeError("Generate with CPython 3.12.10, optimization level 0")
    records = {}
    entries = json.loads(Path(plan).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="verification-hotfix-") as temp:
        for module, target in sorted(hotfix_pairs({"hotfix": entries})):
            if git("cat-file", "-e", f"{target}^{{commit}}", check=False).returncode:
                git("fetch", "--no-tags", "origin", target)
            commit = git("rev-parse", f"{target}^{{commit}}").stdout.decode().strip()
            source_path = "src/" + module.replace(".", "/") + ".py"
            source = git("show", f"{commit}:{source_path}").stdout
            local_source, local_pyc = Path(temp) / "module.py", Path(temp) / "module.pyc"
            local_source.write_bytes(source)
            py_compile.compile(str(local_source), cfile=str(local_pyc),
                               dfile=source_path, doraise=True, optimize=0)
            record = {"module": "src." + module,
                      "marshal_sha256": marshal_sha256_from_pyc(local_pyc),
                      "source_sha256": source_sha256(source), "commit_sha": commit}
            records.setdefault(record_key(record), record)
    write_records(output, records.values())
    print(f"Generated {len(records)} hotfix records")


def merge_records(existing, additions):
    merged = {}
    for record in [*existing, *additions]:
        if not isinstance(record, dict) or not record.get("module") or not record.get("source_sha256"):
            raise ValueError("Malformed verification record; refusing to modify catalog")
        merged.setdefault(record_key(record), record)
    return list(merged.values())


def publish(delta):
    if not Path(delta).is_file():
        raise FileNotFoundError(f"Publication delta not found: {delta}")
    additions = load_existing_records(Path(delta))
    if not additions:
        print("No new verification records to publish")
        return
    with tempfile.TemporaryDirectory(prefix="verification-publish-") as temp:
        worktree = Path(temp) / "checkout"
        try:
            for attempt in range(6):
                git("fetch", "--no-tags", "origin", "main")
                head = git("rev-parse", "FETCH_HEAD").stdout.decode().strip()
                if not worktree.exists():
                    git("worktree", "add", "--detach", str(worktree), head)
                else:
                    git("reset", "--hard", head, cwd=worktree)
                path = worktree / CATALOG
                if not path.is_file():
                    raise FileNotFoundError("Remote verification catalog is missing; refusing to replace it")
                existing = load_existing_records(path)
                merged = merge_records(existing, additions)
                if {record_key(r) for r in merged} == {record_key(r) for r in existing}:
                    print("All publication records already present; nothing to commit")
                    return
                write_records(path, merged)
                git("add", CATALOG, cwd=worktree)
                git("-c", "user.name=github-actions[bot]", "-c",
                    "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                    "commit", "-m", "chore: accumulate published verification records", cwd=worktree)
                result = git("push", "origin", "HEAD:refs/heads/main", cwd=worktree, check=False)
                if result.returncode == 0:
                    print(f"Accumulated {len(merged) - len(existing)} records in {CATALOG}")
                    return
                if attempt < 5:
                    time.sleep(2 * (attempt + 1))
            raise RuntimeError("Unable to publish verification delta after 6 fast-forward attempts")
        finally:
            if worktree.exists():
                git("worktree", "remove", "--force", str(worktree), check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan-hotfix")
    plan.add_argument("--before", required=True)
    plan.add_argument("--after", required=True)
    plan.add_argument("--output", required=True)
    generate = commands.add_parser("generate-hotfix")
    generate.add_argument("--plan", required=True)
    generate.add_argument("--output", required=True)
    publication = commands.add_parser("publish")
    publication.add_argument("--delta", required=True)
    args = parser.parse_args()
    if args.command == "plan-hotfix":
        plan_hotfix(args.before, args.after, args.output)
    elif args.command == "generate-hotfix":
        generate_hotfix(args.plan, args.output)
    else:
        publish(args.delta)


if __name__ == "__main__":
    main()
