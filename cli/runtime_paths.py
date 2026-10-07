from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional


def resolve_dotnet(preferred: Optional[Path] = None) -> Path:
    """Resolve the dotnet host on Windows or Linux.

    ``DOTNET_EXE`` is the explicit override. ``DOTNET_ROOT`` and PATH follow,
    then the conventional per-user and Windows installation locations.
    A stale platform-specific preferred path is ignored instead of preventing
    an otherwise valid installation from being found.
    """
    executable = "dotnet.exe" if os.name == "nt" else "dotnet"
    candidates: list[Path] = []

    if preferred is not None:
        candidates.append(Path(preferred).expanduser())

    explicit = os.environ.get("DOTNET_EXE")
    if explicit:
        candidates.append(Path(explicit).expanduser())

    dotnet_root = os.environ.get("DOTNET_ROOT")
    if dotnet_root:
        candidates.append(Path(dotnet_root).expanduser() / executable)

    on_path = shutil.which("dotnet")
    if on_path:
        candidates.append(Path(on_path))

    if os.name == "nt":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        candidates.append(Path(program_files) / "dotnet" / "dotnet.exe")

    candidates.extend(
        [
            Path.home() / ".dotnet" / executable,
            Path.home() / ".dotnet-arm64" / executable,
        ]
    )

    checked: list[str] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        label = str(resolved).lower() if os.name == "nt" else str(resolved)
        if label in checked:
            continue
        checked.append(label)
        if resolved.is_file():
            return resolved

    details = "\n  - ".join(checked) if checked else "(no candidates)"
    raise FileNotFoundError(
        "Unable to find dotnet. Install the .NET 9 SDK or set DOTNET_EXE. "
        f"Checked:\n  - {details}"
    )
