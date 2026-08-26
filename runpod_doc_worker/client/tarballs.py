"""Reading a tar safely: member names, quotas, and the standard data filter.

The extraction itself is delegated to ``filter="data"``. What is here is
everything the filter does not do -- the destination-filesystem rules, the
expansion quotas, and a bound on metadata the filter reads before it is consulted.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from runpod_doc_worker.client import limits
from runpod_doc_worker.client.errors import _DECOMPRESSION_ERRORS, ResponseError
from runpod_doc_worker.client.names import (
    _check_member_collisions,
    _check_member_name,
    within,
)


class _MetadataBudget:
    """How much metadata one archive may spend in total, not per header.

    The per-header limit bounds a single allocation. It does not bound the sum:
    a hundred thousand members, each with a PAX value just under the cap, is a
    hundred gigabytes -- and the parsed values are not transient, because
    `tar.next()` keeps every `TarInfo` it has produced until enumeration ends. So
    the earlier fix bounded the wrong quantity and the archive that exploits it is
    the ordinary one with many members.

    Reset per extraction rather than per process, since the budget is a property
    of the response being read.
    """

    __slots__ = ("spent",)

    def __init__(self) -> None:
        self.spent = 0

    def charge(self, size: int) -> None:
        self.spent += size
        if self.spent > limits.MAX_TOTAL_METADATA_BYTES:
            raise ResponseError(
                f"refusing the archive: its members declare over "
                f"{limits.MAX_TOTAL_METADATA_BYTES} bytes of metadata in total"
            )


class _BoundedTarInfo(tarfile.TarInfo):
    """A member whose metadata is bounded *before* it is read.

    `tar.next()` processes a PAX extended header or a GNU long-name block on the
    way to the member it describes: it reads the whole declared size into memory
    and returns the real member afterwards. Both quotas below run on what `next()`
    returns, so neither had happened yet -- a 10 KB gzip could make one call
    materialise a 10 MB value while the only member it announced had size zero,
    and the size field is twelve octal digits, so far larger is expressible. The
    failure then arrives as a bare `MemoryError`, outside this module's one-error
    contract.

    Bounding it after the fact is no bound at all, since the allocation is the
    harm. So the check happens in `_proc_member`, which is where `tarfile`
    dispatches on the member type and therefore the one place ahead of all three
    metadata readers. Guarding `_proc_pax` alone would have left the two GNU
    long-name types reachable -- the same "fixed one of the call sites" shape that
    has repeatedly turned out to cover one caller and miss the other.
    """

    def _proc_member(self, archive):
        if self.type in limits._TAR_METADATA_TYPES:
            if self.size > limits.MAX_METADATA_BYTES:
                raise ResponseError(
                    f"refusing tar member metadata of {self.size} bytes, over "
                    f"the {limits.MAX_METADATA_BYTES}-byte limit"
                )
            # And against the running total, before the read rather than after.
            budget = getattr(archive, "_runpod_metadata_budget", None)
            if budget is not None:
                budget.charge(self.size)
        return super()._proc_member(archive)


def _open_tar(data: bytes) -> tarfile.TarFile:
    """Open a tar with the bounded parser. The only place that opens one.

    It became the only place because there had been two. The metadata bound is
    installed by passing ``tarinfo=``, so it applied to extraction and not to the
    ``is_tarfile`` call that *detects* a tar -- and detection parses the first
    member, which is exactly where an oversized PAX or GNU long-name block sits.
    A 2,180-byte gzip decompressed 2,098,688 bytes there before the limit further
    down could refuse anything, so the bound existed and the archive that defeats
    it never reached it.

    That is the same "fixed one of two call sites" shape as six earlier findings in
    this review, and the answer is the same: one producer, so the next thing added
    here cannot apply to one caller and miss the other.
    """
    archive = tarfile.open(
        fileobj=io.BytesIO(data), mode="r:*", tarinfo=_BoundedTarInfo
    )
    # Carried on the archive rather than in a module global, so two concurrent
    # extractions cannot spend each other's budget.
    archive._runpod_metadata_budget = _MetadataBudget()
    return archive


def _looks_like_tar(data: bytes) -> bool:
    """Whether this is a tar, decided by the bounded parser.

    Reimplements ``tarfile.is_tarfile``'s contract rather than calling it, because
    that function offers no way to pass ``tarinfo=``. The contract copied is the
    error split, which matters: a ``TarError`` means "not a tar" and a
    decompression or OS error means "unreadable", and the caller renders those
    differently. A ``ResponseError`` from the metadata bound is neither, so it
    propagates -- refusing at detection, which is the point.
    """
    try:
        with _open_tar(data):
            return True
    except tarfile.TarError:
        return False


def _extract_tar(data: bytes, destination: Path) -> None:
    """Extract a tar, refusing members that escape or are not regular files.

    Checked before extracting rather than relying on the stdlib filter alone, so
    the guarantee does not depend on the Python patch release. The ``data`` filter
    is then used where available for defence in depth and to avoid the 3.14
    default-filter deprecation.
    """
    try:
        tar_file = _open_tar(data)
    except (
        tarfile.TarError,
        OSError,
        TypeError,
        ValueError,
        OverflowError,
        *_DECOMPRESSION_ERRORS,
    ) as e:
        # A truncated download, or a body that was never a tar. Raised before any
        # of the safety checks below could run, so it used to bypass them and the
        # client's error type together.
        raise ResponseError(f"the archive could not be read: {e}") from e

    with tar_file as tar:
        try:
            # Incremental, not `getmembers()`. That decompresses the whole
            # stream and materialises every TarInfo before either quota can be
            # read, so a tiny gzip declaring millions of empty headers exhausts
            # memory before `len(members)` is even reached. Aborting mid-walk
            # means the cost of a hostile archive is bounded by the quota rather
            # than by the archive.
            members = []
            declared_total = 0
            while True:
                member = tar.next()
                if member is None:
                    break
                members.append(member)
                if len(members) > limits.MAX_ARCHIVE_MEMBERS:
                    raise ResponseError(
                        f"the archive declares over {limits.MAX_ARCHIVE_MEMBERS} members"
                    )
                declared_total += member.size
                if declared_total > limits.MAX_EXTRACTED_BYTES:
                    raise ResponseError(
                        f"the archive expands to over {limits.MAX_EXTRACTED_BYTES} bytes"
                    )
        except (
            tarfile.TarError,
            OSError,
            TypeError,
            ValueError,
            OverflowError,
            *_DECOMPRESSION_ERRORS,
        ) as e:
            # A tar truncated after a valid first header opens cleanly and fails
            # here, so the extraction handler below was never reached.
            #
            # Enumerating a *compressed* tar decompresses the whole stream, which
            # makes this — not the extraction below — where damage deep inside an
            # xz or gzip stream actually surfaces, as `LZMAError` or `zlib.error`.
            # Widening only the extraction handler left this case escaping, which
            # is why the fix was verified by reproduction rather than by reading.
            raise ResponseError(f"the archive could not be read: {e}") from e

        _check_member_collisions([m.name for m in members], container="tar", destination=destination)
        for member in members:
            _check_member_name(member.name, container="tar")
            if not (member.isfile() or member.isdir()):
                raise ResponseError(
                    f"refusing unsafe tar member {member.name!r} "
                    f"(not a regular file or dir)"
                )
            if not within(destination, member.name):
                raise ResponseError(
                    f"refusing tar member {member.name!r}: path escapes the destination"
                )
        try:
            # `filter="data"` and nothing else. It is the standard library's own
            # hardening -- setuid/setgid and world-writable bits stripped,
            # archive-supplied ownership discarded, links and special files
            # refused -- and it exists in every interpreter this distribution now
            # supports, which is the whole reason the floor was raised to 3.10.12.
            #
            # There used to be a filterless fallback here, 88 lines re-deriving
            # those permission rules for 3.10.0-3.10.11. It was a defect source in
            # its own right -- the usable-mode mask, the umask read
            # racing other threads, an inherited setgid bit, ownership defaulting
            # to root, `None` meaning "leave alone" to the filter and "crash" to
            # the older `os.chmod` -- because it was a second implementation of
            # security-relevant behaviour, kept in step with the first by hand.
            # Deleting it removes that whole class of defect rather than the six
            # instances of it, and costs eleven patch releases from June 2023.
            tar.extractall(destination, filter="data")
        except (
            tarfile.TarError,
            OSError,
            TypeError,
            ValueError,
            OverflowError,
            *_DECOMPRESSION_ERRORS,
        ) as e:
            # TarError covers truncation, which is only discovered on read for a
            # streamed member. OSError covers the destination refusing the write —
            # a file member landing where a directory already exists raises
            # IsADirectoryError or PermissionError, which describes a response this
            # code cannot use rather than a bug in the caller. The zip path below
            # already caught OSError; this one did not.
            #
            # ValueError and OverflowError come from the timestamp: a PAX `mtime`
            # of `nan`, or one outside the platform's time_t range, reaches
            # `os.utime` and raises there. Neither the checks above nor the `data`
            # filter inspects mtime, so this covers the modern path as much as the
            # fallback -- and extraction has written files by then, which is the
            # more uncomfortable half of it.
            #
            # The decompression errors are here for the case `tarfile.open` cannot
            # see: corruption deep in a compressed stream. The header decompresses,
            # the archive opens, and the damage surfaces only when extraction reads
            # that far — as `LZMAError` for an xz tar, verified escaping this
            # handler before the tuple was widened.
            raise ResponseError(f"the archive could not be extracted: {e}") from e
