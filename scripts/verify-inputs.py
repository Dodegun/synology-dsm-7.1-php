"""Check every pinned source before running any source build script."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
lock = json.loads((root / "sources.lock.json").read_text("utf-8"))
items = lock.get("sources", lock)
if isinstance(items, dict):
    items = [dict(value, name=name) for name, value in items.items()]
for row in items:
    source = root / "downloads" / row["filename"]
    expected = row.get("sha256") or row.get("expected_sha256") or row.get("observed_sha256")
    if not expected:
        raise ValueError(f"No hash pinned for {source.name}")
    observed = hashlib.file_digest(source.open("rb"), "sha256").hexdigest()
    if observed.lower() != expected.lower():
        raise ValueError(f"Checksum mismatch for {source.name}")
    print(f"SHA256 verified {source.name}")
