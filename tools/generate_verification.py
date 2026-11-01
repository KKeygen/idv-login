import argparse
import hashlib
import importlib.util
import json
import marshal
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_sha256(data: bytes) -> str:
    return sha256_bytes(data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))


def module_from_py(src_root: Path, file_path: Path) -> str:
    rel = file_path.relative_to(src_root)
    return "src." + ".".join(rel.with_suffix("").parts)


def module_from_non_py(src_root: Path, file_path: Path) -> str:
    return file_path.name


def pyc_path_for_source(py_path: Path) -> Path:
    return Path(importlib.util.cache_from_source(str(py_path)))


def normalize_code_object(code_obj, filename: str = "<normalized>"):
    new_consts = []
    for const in code_obj.co_consts:
        if isinstance(const, type(code_obj)):
            new_consts.append(normalize_code_object(const, filename))
        else:
            new_consts.append(const)
    return code_obj.replace(co_filename=filename, co_consts=tuple(new_consts))


def marshal_sha256_from_pyc(pyc_path: Path) -> str:
    # CPython 3.7+ pyc header size is 16 bytes.
    data = pyc_path.read_bytes()
    if len(data) < 16:
        raise ValueError(f"Invalid pyc file: {pyc_path}")
    code_obj = marshal.loads(data[16:])
    normalized = normalize_code_object(code_obj)
    return sha256_bytes(marshal.dumps(normalized))


def load_existing_records(path: Path) -> list:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid verification catalog: {path}") from exc
    if not isinstance(data, list):
        raise ValueError(f"Verification catalog must be a list: {path}")
    return data


def record_key(record: dict) -> tuple[str, str, str]:
    return (
        str(record.get("module", "")),
        str(record.get("marshal_sha256", "")),
        str(record.get("source_sha256", "")),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate verification records from src and pyc files.")
    parser.add_argument("--src-root", default="src", help="Source directory root.")
    parser.add_argument("--start-bat", default="tools/点我启动工具.bat", help="Start bat path to record.")
    parser.add_argument("--verification-file", default="ext/verification.json", help="Output verification json path.")
    parser.add_argument("--commit-sha", required=True, help="Git commit sha for current build.")
    parser.add_argument("--staged-root", help="Final shipped src directory; include only its pyc files.")
    args = parser.parse_args()

    src_root = Path(args.src_root).resolve()
    start_bat = Path(args.start_bat).resolve()
    verification_file = Path(args.verification_file).resolve()

    existing = load_existing_records(verification_file)
    latest_by_key: dict[tuple[str, str, str], dict] = {}
    for item in existing:
        if not isinstance(item, dict) or not item.get("module") or not item.get("source_sha256"):
            raise ValueError("Malformed existing verification record; refusing to replace catalog")
        latest_by_key.setdefault(record_key(item), item)

    staged_root = Path(args.staged_root).resolve() if args.staged_root else None
    if staged_root is not None:
        if not staged_root.is_dir():
            raise FileNotFoundError(f"Staged source directory not found: {staged_root}")
        source_files = [src_root / p.relative_to(staged_root).with_suffix(".py")
                        for p in sorted(staged_root.rglob("*.pyc")) if "__pycache__" not in p.parts]
        if not source_files:
            raise ValueError("The staged release contains no application pyc files")
    else:
        source_files = sorted(p for p in src_root.rglob("*.py") if p.is_file())

    for file_path in source_files:
        rel_parts = file_path.relative_to(src_root).parts
        if "__pycache__" in rel_parts:
            continue

        source_hash = source_sha256(file_path.read_bytes())

        pyc_path = (staged_root / file_path.relative_to(src_root).with_suffix(".pyc")
                    if staged_root is not None else pyc_path_for_source(file_path))
        if not pyc_path.exists():
            raise FileNotFoundError(f"Missing compiled pyc for {file_path}: expected {pyc_path}")

        record = {
            "module": module_from_py(src_root, file_path),
            "marshal_sha256": marshal_sha256_from_pyc(pyc_path),
            "source_sha256": source_hash,
            "commit_sha": args.commit_sha,
        }

        latest_by_key.setdefault(record_key(record), record)

    if start_bat.exists() and start_bat.is_file():
        bat_record = {
            "module": module_from_non_py(src_root, start_bat),
            "source_sha256": sha256_bytes(start_bat.read_bytes()),
            "commit_sha": args.commit_sha,
        }
        latest_by_key.setdefault(record_key(bat_record), bat_record)
    else:
        raise FileNotFoundError(f"Start bat not found: {start_bat}")

    merged = sorted(
        latest_by_key.values(),
        key=lambda x: (
            str(x.get("module", "")),
            str(x.get("source_sha256", "")),
            str(x.get("marshal_sha256", "")),
        ),
    )
    verification_file.parent.mkdir(parents=True, exist_ok=True)
    verification_file.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote {len(merged)} deduplicated records to {verification_file}")


if __name__ == "__main__":
    main()
