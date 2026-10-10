#!/usr/bin/env python3
import os
import pathlib
import sys
import tarfile

archive_path = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
forbidden = {".deploy-trusted", ".github", ".git", "deploy"}
max_files = 20_000
max_size = 512 * 1024 * 1024

with tarfile.open(archive_path, "r:gz") as archive:
    members = archive.getmembers()
    if not 0 < len(members) <= max_files:
        raise SystemExit("invalid archive member count")
    total_size = 0
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        parts = tuple(part for part in path.parts if part != ".")
        if not parts and member.isdir():
            continue
        if not parts or path.is_absolute() or ".." in parts or parts[0] in forbidden:
            raise SystemExit(f"forbidden archive path: {member.name}")
        if not (member.isfile() or member.isdir()):
            raise SystemExit(f"unsupported archive member: {member.name}")
        total_size += member.size
    if total_size > max_size:
        raise SystemExit("archive is too large")

    destination.mkdir(mode=0o755)
    for member in members:
        relative = pathlib.PurePosixPath(*(part for part in pathlib.PurePosixPath(member.name).parts if part != "."))
        if not relative.parts:
            continue
        target = destination.joinpath(*relative.parts)
        if member.isdir():
            target.mkdir(mode=0o755, exist_ok=True)
            continue
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with archive.extractfile(member) as source, open(target, "wb") as output:
            assert source is not None
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
        os.chmod(target, 0o644)
