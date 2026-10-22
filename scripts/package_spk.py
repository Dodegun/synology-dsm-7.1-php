#!/usr/bin/env python3
"""Package a PHP runtime for DSM 7.1 cedarview."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import tempfile
from typing import BinaryIO


PHP_SERIES = os.environ.get("PHP_SERIES", "8.4")
if PHP_SERIES not in {"8.2", "8.3", "8.4"}:
    raise ValueError("PHP_SERIES must be 8.2, 8.3 or 8.4")
_versions_path = Path(__file__).resolve().parents[1] / "versions.json"
_versions_document = json.loads(_versions_path.read_text("utf-8"))
_versions = _versions_document.get("versions", _versions_document)
BUILD_VERSION = _versions[PHP_SERIES]
PHP_VERSION = BUILD_VERSION["version"]
if not re.fullmatch(re.escape(PHP_SERIES) + r"\.[0-9]+", PHP_VERSION):
    raise ValueError("Selected versions.json version does not match PHP_SERIES")
METADATA_ID = BUILD_VERSION["metadata_id"]
if not isinstance(METADATA_ID, int) or isinstance(METADATA_ID, bool):
    raise ValueError("Selected metadata_id must be an integer")
PACKAGE = f"PHP{PHP_SERIES}"
PHP_PREFIX = f"php{PHP_SERIES.replace('.', '')}"
TARGET = f"/var/packages/{PACKAGE}/target"
MODULE_DIR = f"usr/local/lib/{PHP_PREFIX}/modules"
REQUIRED_BINARIES = (PHP_PREFIX, f"{PHP_PREFIX}-cgi", f"{PHP_PREFIX}-fpm")
REQUIRED_CACHE_MODULES = ("opcache", "apcu")
ZEND_MODULES = {"opcache", "xdebug"}
SPECIAL_WS_MODULES = ZEND_MODULES | {"apcu"}
MAX_INPUT_BYTES = 1024 * 1024 * 1024
MODULE_LOAD_RE = re.compile(r"^\s*(?:zend_extension|extension)\s*=", re.IGNORECASE)
GENERATED_MODULE_INIS = {"extension.ini", "extensions.ini", "cache.ini"}
REQUIRED_INI = {
    "extension_dir": f"{TARGET}/{MODULE_DIR}",
    "curl.cainfo": f"{TARGET}/etc/cacert.pem",
    "openssl.cafile": f"{TARGET}/etc/cacert.pem",
    "opcache.jit": "0",
    "opcache.jit_buffer_size": "0",
    "session.upload_progress.enabled": "0",
    "session.save_path": "/var/services/tmp",
}
TEMPLATE_VALUES = {
    "PHP_SERIES": PHP_SERIES,
    "PHP_VERSION": PHP_VERSION,
    "PACKAGE": PACKAGE,
    "PHP_PREFIX": PHP_PREFIX,
    "TARGET": TARGET,
    "MODULE_DIR": MODULE_DIR,
    "METADATA_ID": str(METADATA_ID),
}


def render_template(text: str) -> str:
    """Render explicit package tokens without shell expansion or broad rewrites."""
    def replace(match: re.Match) -> str:
        try:
            return TEMPLATE_VALUES[match.group(1)]
        except KeyError as error:
            raise ValueError(f"Unknown package template token: {match.group(0)}") from error
    return re.sub(r"@([A-Z][A-Z0-9_]*)@", replace, text)


def safe_name(name: str) -> str:
    """Accept canonical relative POSIX archive names only."""
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or ":" in name
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
        or any(p in ("", ".", "..") for p in name.split("/"))
        or str(PurePosixPath(name)) != name
    ):
        raise ValueError(f"Unsafe archive path: {name!r}")
    return name


def symlink_destination(name: str, target: str) -> str:
    safe_name(name)
    if (
        not target
        or target.startswith("/")
        or "\\" in target
        or ":" in target
        or any(ord(c) < 32 or ord(c) == 127 for c in target)
    ):
        raise ValueError(f"Unsafe symlink target for {name!r}: {target!r}")
    parts = name.split("/")[:-1]
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ValueError(f"Escaping symlink: {name!r} -> {target!r}")
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        raise ValueError(f"Symlink resolves to archive root: {name!r}")
    return safe_name("/".join(parts))


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str
    mode: int = 0o644
    source: Path | None = None
    data: bytes | None = None
    target: str | None = None

    def size(self) -> int:
        if self.kind != "file":
            return 0
        return len(self.data) if self.data is not None else self.source.stat().st_size

    def open(self) -> BinaryIO:
        if self.kind != "file":
            raise ValueError("Only regular files have data streams")
        return io.BytesIO(self.data) if self.data is not None else self.source.open("rb")


def collect_tree(root: Path, *, scaffold: bool = False) -> dict[str, Entry]:
    result: dict[str, Entry] = {}

    def visit(directory: Path, prefix: str = "") -> None:
        for item in sorted(directory.iterdir(), key=lambda p: p.name):
            name = safe_name(f"{prefix}/{item.name}" if prefix else item.name)
            info = item.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if mode & 0o6000:
                raise ValueError(f"Set-ID file rejected: {name}")
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(item)
                symlink_destination(name, target)
                result[name] = Entry(name, "symlink", mode or 0o777, target=target)
            elif stat.S_ISDIR(info.st_mode):
                result[name] = Entry(name, "directory", 0o755 if scaffold else mode)
                visit(item, name)
            elif stat.S_ISREG(info.st_mode):
                if info.st_size > MAX_INPUT_BYTES:
                    raise ValueError(f"Oversized input file: {name}")
                if scaffold:
                    mode = 0o644
                elif os.name == "nt":
                    # NTFS does not preserve POSIX execute bits. Preserve the
                    # executable intent for ELF objects and script shebangs.
                    with item.open("rb") as stream:
                        head = stream.read(4)
                    mode = 0o755 if head.startswith((b"\x7fELF", b"#!")) else 0o644
                result[name] = Entry(name, "file", mode, source=item)
            else:
                raise ValueError(f"Device/FIFO/socket input rejected: {name}")

    visit(root)
    return result


def add_parents(entries: dict[str, Entry]) -> None:
    for name in list(entries):
        parent = PurePosixPath(name).parent
        while str(parent) != ".":
            key = str(parent)
            old = entries.get(key)
            if old is not None and old.kind != "directory":
                raise ValueError(f"Non-directory archive ancestor: {key}")
            entries.setdefault(key, Entry(key, "directory", 0o755))
            parent = parent.parent


def validate_entries(entries: dict[str, Entry]) -> None:
    for name, entry in entries.items():
        safe_name(name)
        if entry.name != name or entry.kind not in {"file", "directory", "symlink"}:
            raise ValueError(f"Invalid archive entry: {name}")
        if entry.mode & ~0o777:
            raise ValueError(f"Unsupported permission bits: {name}")
        for parent in PurePosixPath(name).parents:
            if str(parent) == ".":
                continue
            if str(parent) in entries and entries[str(parent)].kind != "directory":
                raise ValueError(f"Archive path traverses a symlink/file: {name}")
        if entry.kind != "symlink":
            continue
        destination = symlink_destination(name, entry.target)
        seen = {name}
        for _ in range(len(entries) + 1):
            if destination in seen:
                raise ValueError(f"Cyclic symlink: {name}")
            seen.add(destination)
            target = entries.get(destination)
            if target is None:
                raise ValueError(f"Dangling symlink: {name} -> {entry.target}")
            if target.kind != "symlink":
                break
            destination = symlink_destination(destination, target.target)
        else:
            raise ValueError(f"Symlink resolution exceeds entry count: {name}")


def file_digest(entry: Entry, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with entry.open() as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_entries(entries: dict[str, Entry]) -> list[dict]:
    result = []
    for name, entry in sorted(entries.items()):
        item = {"path": name, "kind": entry.kind, "mode": f"{entry.mode:04o}"}
        if entry.kind == "file":
            item.update(size=entry.size(), sha256=file_digest(entry))
        elif entry.kind == "symlink":
            item["target"] = entry.target
        result.append(item)
    return result


def write_tar(destination: BinaryIO, entries: dict[str, Entry], epoch: int) -> None:
    validate_entries(entries)
    with tarfile.open(fileobj=destination, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, entry in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = epoch
            info.mode = entry.mode
            if entry.kind == "directory":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif entry.kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = entry.target
                archive.addfile(info)
            else:
                info.size = entry.size()
                with entry.open() as stream:
                    archive.addfile(info, stream)


def json_entry(name: str, value: object) -> Entry:
    return Entry(name, "file", data=(json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def resolve_entry(entries: dict[str, Entry], name: str) -> Entry:
    entry = entries[name]
    while entry.kind == "symlink":
        entry = entries[symlink_destination(entry.name, entry.target)]
    return entry


def entry_text(entries: dict[str, Entry], name: str) -> str:
    entry = resolve_entry(entries, name)
    if entry.kind != "file":
        raise ValueError(f"Expected a regular configuration file: {name}")
    with entry.open() as stream:
        return stream.read().decode("utf-8-sig").replace("\r\n", "\n")


def runtime_entries(staged: dict[str, Entry]) -> dict[str, Entry]:
    """Ship runtime files only, retaining complete notices and provenance."""
    executables = {f"usr/local/bin/{binary}" for binary in REQUIRED_BINARIES}
    configs = {"etc/php.ini", "etc/cacert.pem", "etc/ssl/openssl.cnf"}

    def allowed(name: str) -> bool:
        path = PurePosixPath(name)
        return (
            name in executables
            or name in configs
            or (str(path.parent) == "etc/conf.d" and name.endswith(".ini"))
            or (str(path.parent) == MODULE_DIR and name.endswith(".so"))
            or name.startswith(("misc/", "share/licenses/"))
            or (name.startswith(("usr/lib/", "usr/local/lib/"))
                and re.fullmatch(r"[^/]+\.so(?:\.[A-Za-z0-9_-]+)*", path.name) is not None)
        )

    result = {name: entry for name, entry in staged.items()
              if entry.kind != "directory" and allowed(name)}
    # Keep the genuine target behind a selected runtime symlink. Validation of
    # the full input already rejects escaping, missing and cyclic links.
    pending = list(result)
    while pending:
        name = pending.pop()
        entry = result[name]
        if entry.kind != "symlink":
            continue
        destination = symlink_destination(name, entry.target)
        target = staged[destination]
        if target.kind == "directory" or not allowed(destination):
            raise ValueError(f"Runtime symlink points outside the runtime file set: {name}")
        if destination not in result:
            result[destination] = target
            pending.append(destination)
    add_parents(result)
    validate_entries(result)
    return result


def canonical_ini(text: str, *, name: str) -> bytes:
    """Retain the production template while enforcing package path defaults."""
    lines = []
    for line in text.splitlines():
        if MODULE_LOAD_RE.match(line):
            raise ValueError(f"Module load belongs in generated conf.d/extension.ini, not {name}")
        match = re.match(r"^\s*([A-Za-z0-9_.]+)\s*=", line)
        if match and match.group(1).lower() in REQUIRED_INI:
            continue
        lines.append(line)
    lines.extend(("", f"; DSM 7.1 {PACKAGE} package defaults.", "[PHP]"))
    lines.extend(f"{key} = {value}" for key, value in REQUIRED_INI.items())
    assembled = "\n".join(lines).rstrip() + "\n"
    reject_other_series_paths(assembled, name)
    return assembled.encode()


def reject_other_series_paths(text: str, name: str) -> None:
    for series in ("8.2", "8.3", "8.4"):
        if series == PHP_SERIES:
            continue
        prefix = "php" + series.replace(".", "")
        if f"/var/packages/PHP{series}/" in text or f"/usr/local/lib/{prefix}/" in text or f"/usr/local/bin/{prefix}" in text:
            raise ValueError(f"Configuration {name} contains paths for PHP {series}, selected PHP {PHP_SERIES}")


def normalize_module_inis(entries: dict[str, Entry]) -> None:
    for name in list(entries):
        path = PurePosixPath(name)
        if str(path.parent) != "etc/conf.d" or not name.endswith(".ini"):
            continue
        text = entry_text(entries, name)
        has_loads = any(MODULE_LOAD_RE.match(line) for line in text.splitlines())
        if has_loads and path.name not in GENERATED_MODULE_INIS:
            raise ValueError(f"Unexpected active module load in {name}")
        if has_loads:
            lines = [line for line in text.splitlines() if not MODULE_LOAD_RE.match(line)]
            entries[name] = Entry(name, "file", data=("\n".join(lines).rstrip() + "\n").encode())


def validate_runtime(entries: dict[str, Entry]) -> list[str]:
    for binary in REQUIRED_BINARIES:
        name = f"usr/local/bin/{binary}"
        if name not in entries:
            raise ValueError(f"Missing staged runtime binary: {name}")
        entry = resolve_entry(entries, name)
        if entry.kind != "file" or not entry.mode & 0o111:
            raise ValueError(f"Staged runtime is not executable: {name}")
        with entry.open() as stream:
            header = stream.read(20)
        if header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\x3e\x00":
            raise ValueError(f"Expected an ELF64 little-endian x86-64 runtime: {name}")
    modules = sorted(
        PurePosixPath(name).stem
        for name, entry in entries.items()
        if str(PurePosixPath(name).parent) == MODULE_DIR
        and name.endswith(".so")
        and entry.kind in {"file", "symlink"}
    )
    for module in modules:
        if not re.fullmatch(r"[A-Za-z0-9_]+", module):
            raise ValueError(f"Unsafe module name: {module}")
    missing = set(REQUIRED_CACHE_MODULES) - set(modules)
    if missing:
        raise ValueError(f"Web Station cache option requires staged modules: {sorted(missing)}")
    return modules


def prepare_payload(stage: Path, scaffold: Path, settings: Path | None) -> tuple[dict[str, Entry], list[str]]:
    staged = collect_tree(stage)
    add_parents(staged)
    validate_entries(staged)
    entries = runtime_entries(staged)
    modules = validate_runtime(entries)
    for required in ("etc/php.ini", "etc/cacert.pem", "etc/ssl/openssl.cnf"):
        if required not in entries or resolve_entry(entries, required).kind != "file":
            raise ValueError(f"Missing staged runtime configuration: {required}")
    if b"-----BEGIN CERTIFICATE-----" not in entry_text(entries, "etc/cacert.pem").encode():
        raise ValueError("Staged CA bundle does not contain PEM certificates")
    production_ini = entry_text(entries, "etc/php.ini")
    staged_settings = entries.get("misc/default_settings.json")
    scaffold_entries = collect_tree(scaffold / "payload", scaffold=True)
    for name, entry in scaffold_entries.items():
        if entry.kind == "file":
            with entry.open() as stream:
                data = render_template(stream.read().decode("utf-8-sig")).encode()
            entries[name] = Entry(name, "file", entry.mode, data=data)
        else:
            entries[name] = entry
    # Both CLI and Web Station FPM start from the same complete production INI.
    # Only assembled entries change; the staged build tree stays untouched.
    for name in ("etc/php.ini", "misc/php-fpm.ini"):
        entries[name] = Entry(name, "file", data=canonical_ini(production_ini, name=name))
    normalize_module_inis(entries)
    if settings is not None:
        defaults = json.loads(settings.read_text("utf-8"))
    elif staged_settings is not None and staged_settings.kind == "file":
        with staged_settings.open() as stream:
            defaults = json.load(stream)
    else:
        raise ValueError(f"PHP {PHP_SERIES} requires runtime-generated misc/default_settings.json or --settings-json")
    if not isinstance(defaults, dict) or not all(isinstance(defaults.get(k), dict) for k in ("core", "extensions")):
        raise ValueError("default_settings.json must contain core and extensions objects")
    for group in defaults.values():
        if not isinstance(group, dict) or any(isinstance(v, (dict, list)) for v in group.values()):
            raise ValueError("INI defaults must contain scalar values")
    for key, value in REQUIRED_INI.items():
        group = "core" if key == "extension_dir" else "extensions"
        defaults[group][key] = value
    reject_other_series_paths(json.dumps(defaults), "misc/default_settings.json")
    entries["misc/default_settings.json"] = json_entry("misc/default_settings.json", defaults)
    extension_list = {
        module: {"enable_default": True, "desc": module}
        for module in modules
        if module not in SPECIAL_WS_MODULES
    }
    entries["misc/extension_list.json"] = json_entry("misc/extension_list.json", extension_list)
    lines = ["; Generated from the shared modules actually present in this package."]
    lines.extend(f"zend_extension = {m}.so" for m in modules if m in ZEND_MODULES)
    lines.extend(f"extension = {m}.so" for m in modules if m not in ZEND_MODULES)
    ini = "\n".join(lines) + "\n"
    entries["etc/conf.d/extension.ini"] = Entry("etc/conf.d/extension.ini", "file", data=ini.encode())
    add_parents(entries)
    validate_entries(entries)
    return entries, modules


def build(stage: Path, output: Path, scaffold: Path, version: str, epoch: int, settings: Path | None, force: bool) -> dict:
    stage = stage.resolve(strict=True)
    scaffold = scaffold.resolve(strict=True)
    output = output.resolve()
    if not stage.is_dir():
        raise ValueError("Payload must be a staged directory")
    if output.is_relative_to(stage) or output.is_relative_to(scaffold):
        raise ValueError("Output must be outside the payload and scaffold directories")
    if not re.fullmatch(re.escape(PHP_VERSION) + r"-[0-9]{4,}", version):
        raise ValueError(f"Selected branch packages PHP {PHP_VERSION} only; expected version {PHP_VERSION}-NNNN")
    if epoch < 0:
        raise ValueError("SOURCE_DATE_EPOCH cannot be negative")
    sidecar = Path(str(output) + ".manifest.json")
    checksum = Path(str(output) + ".sha256")
    if not force and any(p.exists() for p in (output, sidecar, checksum)):
        raise FileExistsError("Output already exists; use --force to replace the generated artifacts")
    entries, modules = prepare_payload(stage, scaffold, settings)
    payload_manifest = manifest_entries(entries)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".package-spk-", dir=output.parent) as tmp:
        temporary = Path(tmp).resolve()
        temporary.relative_to(output.parent)
        payload = temporary / "package.tgz"
        with payload.open("wb") as compressed:
            with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=epoch, compresslevel=9) as gz:
                write_tar(gz, entries, epoch)
        payload_entry = Entry("package.tgz", "file", source=payload)
        payload_md5 = file_digest(payload_entry, "md5")
        info = render_template((scaffold / "INFO.template").read_text("utf-8"))
        info = re.sub(r'^version="[^"\n]+"$', f'version="{version}"', info, flags=re.MULTILINE)
        info = info.rstrip() + f'\nchecksum="{payload_md5}"\nextractsize={sum(e.size() for e in entries.values()) // 1024 + 1}\n'
        outer = {"INFO": Entry("INFO", "file", data=info.encode()), "package.tgz": payload_entry}
        for folder in ("conf", "scripts"):
            for name, entry in collect_tree(scaffold / folder, scaffold=True).items():
                key = f"{folder}/{name}"
                mode = 0o755 if folder == "scripts" or entry.kind == "directory" else 0o644
                if entry.kind == "file":
                    with entry.open() as stream:
                        data = render_template(stream.read().decode("utf-8-sig")).encode()
                    outer[key] = Entry(key, "file", mode, data=data)
                else:
                    outer[key] = Entry(key, entry.kind, mode, target=entry.target)
        add_parents(outer)
        archive_path = temporary / "package.spk"
        with archive_path.open("wb") as stream:
            write_tar(stream, outer, epoch)
        archive_digest = file_digest(Entry("package.spk", "file", source=archive_path))
        report = {
            "format": "unsigned-synology-spk",
            "package": PACKAGE,
            "php_series": PHP_SERIES,
            "source_component": BUILD_VERSION["source"],
            "version": version,
            "architecture": "cedarview",
            "minimum_dsm": "7.1-42661",
            "source_date_epoch": epoch,
            "spk_sha256": archive_digest,
            "package_tgz_sha256": file_digest(payload_entry),
            "package_tgz_md5": payload_md5,
            "shared_modules": modules,
            "nas_runtime_verified": False,
            "webstation_registration_verified": False,
            "payload": payload_manifest,
            "spk_members": manifest_entries(outer),
        }
        # Detect staged files changing while the package was assembled.
        if manifest_entries(entries) != payload_manifest:
            raise ValueError("Staged payload changed while it was packaged")
        temporary_report = temporary / "manifest.json"
        temporary_report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", "utf-8")
        temporary_checksum = temporary / "sha256"
        temporary_checksum.write_text(f"{archive_digest}  {output.name}\n", "ascii")
        os.replace(archive_path, output)
        os.replace(temporary_report, sidecar)
        os.replace(temporary_checksum, checksum)
    return {"spk": str(output), "sha256": archive_digest, "manifest": str(sidecar), "modules": modules,
            "nas_runtime_verified": False, "webstation_registration_verified": False}


def self_test() -> None:
    assert render_template("@PACKAGE@ @PHP_PREFIX@ @METADATA_ID@") == (
        f"{PACKAGE} {PHP_PREFIX} {METADATA_ID}"
    )
    for name in ("../evil", "/absolute", "C:/evil", "a\\b", "a/../b", "a//b", "a/./b", "a\x00b"):
        try:
            safe_name(name)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Accepted unsafe name: {name!r}")
    for target in ("../../evil", "/usr/lib/libc.so", "C:/evil", "..\\evil"):
        try:
            symlink_destination("lib/link", target)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Accepted unsafe target: {target!r}")
    good = {
        "usr/lib/real.so.1": Entry("usr/lib/real.so.1", "file", 0o755, data=b"real library"),
        "usr/lib/link.so": Entry("usr/lib/link.so", "symlink", 0o777, target="real.so.1"),
    }
    add_parents(good)
    first, second = io.BytesIO(), io.BytesIO()
    write_tar(first, good, 0)
    write_tar(second, good, 0)
    assert first.getvalue() == second.getvalue()
    with tarfile.open(fileobj=io.BytesIO(first.getvalue())) as archive:
        assert archive.getmember("usr/lib/link.so").issym()
        assert archive.getmember("usr/lib/link.so").linkname == "real.so.1"
        assert archive.getmember("usr/lib/real.so.1").mode == 0o755
        assert archive.getmember("usr/lib/real.so.1").mtime == 0
    bad = {"a": Entry("a", "symlink", 0o777, target="b"), "b": Entry("b", "symlink", 0o777, target="a")}
    try:
        validate_entries(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("Accepted a symlink cycle")
    normalized = {
        "etc/conf.d/extensions.ini": Entry("etc/conf.d/extensions.ini", "file",
            data=b"; preserve comments\nextension=bcmath\n"),
        "etc/conf.d/cache.ini": Entry("etc/conf.d/cache.ini", "file",
            data=b"zend_extension=opcache.so\nextension=apcu.so\nopcache.jit=0\n"),
    }
    normalize_module_inis(normalized)
    assert normalized["etc/conf.d/extensions.ini"].data == b"; preserve comments\n"
    assert normalized["etc/conf.d/cache.ini"].data == b"opcache.jit=0\n"
    normalized["etc/conf.d/custom.ini"] = Entry("etc/conf.d/custom.ini", "file", data=b"extension=curl\n")
    try:
        normalize_module_inis(normalized)
    except ValueError:
        pass
    else:
        raise AssertionError("Accepted unexpected module-load configuration")
    original_ini = "; production template\nmemory_limit=256M\ncurl.cainfo=/old\n[Date]\n; comments stay\n"
    final_ini = canonical_ini(original_ini, name="etc/php.ini").decode()
    assert "memory_limit=256M\n" in final_ini and "; comments stay\n" in final_ini
    assert final_ini.count("curl.cainfo") == 1 and "/old" not in final_ini
    for key, value in REQUIRED_INI.items():
        assert f"{key} = {value}\n" in final_ini
    filtered = runtime_entries({
        f"usr/local/bin/{PHP_PREFIX}": Entry(f"usr/local/bin/{PHP_PREFIX}", "file", data=b"runtime"),
        "usr/local/bin/phpize84": Entry("usr/local/bin/phpize84", "file", data=b"development"),
        "usr/local/include/php/main.h": Entry("usr/local/include/php/main.h", "file", data=b"header"),
        f"{MODULE_DIR}/opcache.so": Entry(f"{MODULE_DIR}/opcache.so", "file", data=b"module"),
        f"{MODULE_DIR}/opcache.la": Entry(f"{MODULE_DIR}/opcache.la", "file", data=b"libtool"),
        "share/licenses/php/LICENSE": Entry("share/licenses/php/LICENSE", "file", data=b"notice"),
        "misc/sources.lock.json": Entry("misc/sources.lock.json", "file", data=b"{}"),
    })
    assert f"usr/local/bin/{PHP_PREFIX}" in filtered
    assert "share/licenses/php/LICENSE" in filtered and "misc/sources.lock.json" in filtered
    assert not any("phpize" in name or "include/" in name or name.endswith(".la") for name in filtered)
    print("Archive safety, determinism, modes, symlinks and runtime/configuration assembly checks passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path, nargs="?", help="Directory containing the completed runtime payload")
    parser.add_argument("output", type=Path, nargs="?", help="Destination unsigned .spk")
    parser.add_argument("--scaffold", type=Path, default=Path(__file__).resolve().parents[1] / "package")
    parser.add_argument("--settings-json", type=Path, help="Fresh runtime-generated Web Station core/extensions INI defaults")
    parser.add_argument("--version", default=f"{PHP_VERSION}-0001")
    parser.add_argument("--source-date-epoch", type=int, default=int(os.environ.get("SOURCE_DATE_EPOCH", "0")))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.payload is None or args.output is None:
        parser.error("payload and output are required except with --self-test")
    print(json.dumps(build(args.payload, args.output, args.scaffold, args.version, args.source_date_epoch,
                           args.settings_json, args.force), indent=2))


if __name__ == "__main__":
    main()
