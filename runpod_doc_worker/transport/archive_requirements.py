"""Required manifest artifacts that must survive archive packaging."""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Generic, Iterable, Iterator, TypeVar

from runpod_doc_worker.contract import artifacts as _artifacts
from runpod_doc_worker.contract import degraded as _degraded


RequiredMembers = dict[Path, tuple[_artifacts.Artifact, ...]]
_Metadata = TypeVar("_Metadata")

# Keep ordinary small artifacts in memory without letting the largest raw
# archive member double the final archive's memory footprint.
_MEMBER_SPOOL_LIMIT_BYTES = 8 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024


@dataclass
class Snapshot(Generic[_Metadata]):
    """One stable source member, ready to commit to an archive."""

    data: BinaryIO
    metadata: _Metadata
    size: int


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


def _read_chunk(source: BinaryIO) -> bytes:
    return source.read(_COPY_CHUNK_BYTES)


@contextmanager
def capture(
    child: Path,
    required: tuple[_artifacts.Artifact, ...],
    report: _degraded.Report,
    describe: Callable[[BinaryIO], _Metadata],
) -> Iterator[Snapshot[_Metadata] | None]:
    """Stage one source completely before an archive entry is mutated."""
    with tempfile.SpooledTemporaryFile(
        max_size=_MEMBER_SPOOL_LIMIT_BYTES, mode="w+b"
    ) as spool:
        try:
            source = child.open("rb")
        except OSError as exc:
            unreadable(child, exc, required, report)
            yield None
            return

        failure: OSError | None = None
        with source:
            try:
                metadata = describe(source)
            except OSError as exc:
                failure = exc
            if failure is None:
                while True:
                    try:
                        chunk = _read_chunk(source)
                    except OSError as exc:
                        failure = exc
                        break
                    if not chunk:
                        break
                    # Spool failures are infrastructure failures, not evidence
                    # that the optional source member itself was unreadable.
                    spool.write(chunk)

        if failure is not None:
            unreadable(child, failure, required, report)
            yield None
            return

        size = spool.tell()
        spool.seek(0)
        for artifact in required:
            artifact.validate_stream(child, spool, report)
        spool.seek(0)
        yield Snapshot(data=spool, metadata=metadata, size=size)
