"""Required manifest artifacts that must survive archive packaging."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from runpod_doc_worker.contract import artifacts as _artifacts
from runpod_doc_worker.contract import degraded as _degraded


RequiredMembers = dict[Path, tuple[_artifacts.Artifact, ...]]


def select(
    manifest: Iterable[_artifacts.Artifact],
    output_dir: Path,
    basename: str,
    report: _degraded.Report,
) -> RequiredMembers:
    """Select required paths; the archive owns their read and diagnostics."""
    selected: dict[Path, list[_artifacts.Artifact]] = {}
    for artifact in manifest:
        if not artifact.required:
            continue
        unresolvable: set[Path] = set()
        hits = artifact.matches(
            output_dir,
            basename,
            report,
            record_unresolvable=False,
            unresolvable=unresolvable,
        )
        if not hits:
            for path in sorted(unresolvable):
                report.note(
                    reason=_degraded.UNRESOLVABLE,
                    file=path.name,
                    artifact=artifact.key,
                )
            raise _artifacts.ArtifactError(
                f"artifact {artifact.key!r} is required and matched no file. "
                f"Patterns tried, against basename {basename!r}: "
                f"{', '.join(repr(pattern) for pattern in artifact.patterns)}."
            )
        selected.setdefault(hits[0], []).append(artifact)
    return {path: tuple(artifacts) for path, artifacts in selected.items()}


def ensure_included(kept: Iterable[Path], required: RequiredMembers) -> None:
    """Fail when archive filtering rejected a selected required path."""
    kept_set = set(kept)
    for path, artifacts in required.items():
        if path not in kept_set:
            keys = ", ".join(repr(artifact.key) for artifact in artifacts)
            raise _artifacts.ArtifactError(
                f"required artifact {keys} matched {path.name}, which cannot be archived"
            )


def unreadable(
    child: Path,
    exc: OSError,
    required: tuple[_artifacts.Artifact, ...],
    report: _degraded.Report,
) -> None:
    """Report an archive read failure, elevating required members to fatal."""
    artifact = required[0] if required else None
    report.note(
        reason=_degraded.UNREADABLE,
        file=child.name,
        artifact=artifact.key if artifact else None,
        error_type=type(exc).__name__,
    )
    if artifact:
        raise _artifacts.ArtifactError(
            f"required artifact {artifact.key!r} matched {child.name}, "
            f"which could not be archived: {type(exc).__name__}"
        ) from exc


def read(
    child: Path,
    required: tuple[_artifacts.Artifact, ...],
    report: _degraded.Report,
) -> bytes | None:
    """Capture and validate the exact bytes an archive will carry."""
    try:
        data = child.read_bytes()
    except OSError as exc:
        unreadable(child, exc, required, report)
        return None
    for artifact in required:
        artifact.validate_buffered(child, data, report)
    return data
