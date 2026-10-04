"""Vendor a protected cloud artifact; never compile private codec sources here."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

p = argparse.ArgumentParser()
p.add_argument("artifact", type=Path, help="idv-db-wasm GitHub Actions artifact directory")
args = p.parse_args()
manifest = json.loads((args.artifact / "idv-db.manifest.json").read_text())
binary = (args.artifact / "idv-db.wasm").read_bytes()
if not manifest["protected"] or manifest["source_commit"] == "local-development":
    raise SystemExit("Only protected cloud-built Wasm may be distributed")
if manifest["abi"] != 4 or hashlib.sha256(binary).hexdigest() != manifest["sha256"]:
    raise SystemExit("Wasm artifact hash/ABI mismatch")
target = Path(__file__).resolve().parents[1] / "src" / "resources"
target.mkdir(exist_ok=True)
for name in ["idv-db.wasm", "idv-db.manifest.json", "AES-LICENSE.txt", "OBFUSCATOR-LICENSE.txt"]:
    shutil.copyfile(args.artifact / name, target / name)
print("Promoted protected cloud artifact", manifest["source_commit"])
