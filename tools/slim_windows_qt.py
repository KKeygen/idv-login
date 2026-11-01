"Prepare Windows Qt packaging dependencies."

import argparse
import ast
from importlib import metadata
import json
from pathlib import Path
import re

import pefile


AUDITED_IMPORTS = {"QtCore", "QtGui", "QtWidgets"}
PLUGINS = {
    "platforms/qwindows.dll",
    "styles/qmodernwindowsstyle.dll",
    "imageformats/qico.dll",
    "imageformats/qsvg.dll",
}


def platform_condition(node):
    if (isinstance(node, ast.Compare) and len(node.ops) == 1
            and len(node.comparators) == 1
            and isinstance(node.left, ast.Attribute)
            and isinstance(node.left.value, ast.Name)
            and node.left.value.id == "sys" and node.left.attr == "platform"
            and isinstance(node.comparators[0], ast.Constant)
            and isinstance(node.comparators[0].value, str)):
        equal = node.comparators[0].value == "win32"
        if isinstance(node.ops[0], ast.Eq):
            return equal
        if isinstance(node.ops[0], ast.NotEq):
            return not equal
    return None


class WindowsImports(ast.NodeVisitor):
    def __init__(self):
        self.modules = set()

    def visit_If(self, node):
        result = platform_condition(node.test)
        branches = node.body if result is True else node.orelse if result is False else node.body + node.orelse
        for statement in branches:
            self.visit(statement)

    def visit_Import(self, node):
        self.record(item.name for item in node.names)

    def visit_ImportFrom(self, node):
        self.record(("PyQt6." + item.name for item in node.names)
                    if node.module == "PyQt6" else [node.module or ""])

    def record(self, names):
        self.modules.update(name.split(".")[1] for name in names
                            if name.startswith("PyQt6.") and name != "PyQt6.sip")


def source_imports(source_root):
    visitor = WindowsImports()
    for path in source_root.rglob("*.py"):
        if path.name.endswith("_legacy.py") or path.name == "web_uimgr.py":
            continue
        visitor.visit(ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path)))
    if visitor.modules != AUDITED_IMPORTS:
        raise RuntimeError(f"Windows Qt usage changed; re-audit the packaging policy: {sorted(visitor.modules)}")
    return visitor.modules


def binding_closure(qt_root, direct):
    pending, modules = list(direct), set()
    while pending:
        name = pending.pop()
        if name in modules:
            continue
        modules.add(name)
        sip = qt_root / "bindings" / name / f"{name}mod.sip"
        text = sip.read_text(encoding="utf-8-sig")
        pending.extend(re.findall(r"^%Import\s+(\w+)/", text, re.MULTILINE))
    return modules


def pe_closure(qt_root, seeds):
    binaries = {path.name.lower(): path for path in (qt_root / "Qt6/bin").iterdir()
                if path.suffix.lower() in {".dll", ".exe"}}
    pending, keep, edges = list(seeds), set(), {}
    while pending:
        path = pending.pop()
        if path in keep:
            continue
        if not path.is_file():
            raise FileNotFoundError(path)
        keep.add(path)
        with pefile.PE(str(path), fast_load=True) as pe:
            pe.parse_data_directories(directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
            ])
            imports = sorted({entry.dll.decode("ascii").lower()
                              for kind in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT")
                              for entry in getattr(pe, kind, [])})
        edges[path.relative_to(qt_root).as_posix()] = imports
        for name in imports:
            if name in binaries:
                pending.append(binaries[name])
            elif name.startswith("qt6"):
                raise RuntimeError(f"Missing Qt dependency {name} required by {path.name}")
    return keep, edges


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qt-root", required=True, type=Path)
    parser.add_argument("--source-root", default=Path("src"), type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="remove unused files in this staged copy")
    args = parser.parse_args()
    root = args.qt_root.resolve()
    if root.name != "PyQt6" or args.qt_root.is_symlink():
        raise ValueError("--qt-root must be the staged PyQt6 directory")
    before = {path: path.stat().st_size for path in root.rglob("*") if path.is_file()}
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("Symlinks are not expected in Windows wheels")
    direct = source_imports(args.source_root)
    modules = binding_closure(root, direct)
    seeds = {root / f"{name}.pyd" for name in modules}
    seeds.update(root.glob("sip*.pyd"))
    seeds.update(root / "Qt6/plugins" / name for name in PLUGINS)
    keep, edges = pe_closure(root, seeds)
    keep.add(root / "__init__.py")
    if (root / "Qt6/bin/qt.conf").is_file():
        keep.add(root / "Qt6/bin/qt.conf")
    missing = keep - before.keys()
    if missing:
        raise RuntimeError(f"Required Qt files missing: {sorted(str(p) for p in missing)}")
    forbidden = ("webengine", "qml", "quick", "network", "webchannel", "printsupport")
    if any(any(word in path.name.lower() for word in forbidden) for path in keep):
        raise RuntimeError("Unexpected browser/network/QML dependency in raster Widgets closure")
    removed = before.keys() - keep
    versions = {dist.metadata["Name"]: dist.version
                for dist in metadata.distributions(path=[str(root.parent)])
                if dist.metadata["Name"].lower().startswith("pyqt6")}
    report = {
        "applied": args.apply,
        "versions": dict(sorted(versions.items())),
        "direct_modules": sorted(direct), "binding_modules": sorted(modules),
        "before_bytes": sum(before.values()),
        "after_bytes": sum(before[path] for path in keep),
        "saved_bytes": sum(before[path] for path in removed),
        "kept_files": sorted(path.relative_to(root).as_posix() for path in keep),
        "removed_files": {path.relative_to(root).as_posix(): before[path]
                          for path in sorted(removed)},
        "pe_imports": edges,
    }
    if args.apply:
        for path in removed:
            path.unlink()
        for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Qt payload: {report['before_bytes']:,} -> {report['after_bytes']:,} bytes "
          f"({report['saved_bytes']:,} removed; applied={args.apply})")


if __name__ == "__main__":
    main()
