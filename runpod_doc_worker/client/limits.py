"""The caps and timeouts, in one place because a caller may want to raise them.

Read through the module rather than imported by value -- ``limits.MAX_ARCHIVE_BYTES``
and not ``from .limits import MAX_ARCHIVE_BYTES`` -- so that setting one at runtime
takes effect. Binding the value at import time would leave every call site holding
the number it read first, which makes the documented "raise it if you need to"
quietly untrue.
"""

from __future__ import annotations

import tarfile

# Socket timeout for archive downloads — long enough for a slow CDN or a large
# output, short enough that a dead URL cannot hang a caller forever. Mirrors the
# worker-side fetch timeout in runpod_doc_worker.transport.io.
DOWNLOAD_TIMEOUT_SECONDS = 120.0

# Total wall-clock a fetch may take. The socket timeout above bounds only *idle*
# time and is reset by every successful read, so a peer trickling a few bytes
# before each timeout can hold the call open indefinitely without ever
# approaching the byte cap. Generous enough for a large archive over a slow link.
DOWNLOAD_DEADLINE_SECONDS = 900.0

# Caps on what a response may expand to. `extract` reads the archive into memory,
# so an unbounded body is a memory-exhaustion vector on its own, and a small
# archive can still expand to an unbounded amount of disk. Deliberately generous:
# these are backstops against a hostile or broken worker, not a policy on document
# size. A caller needing more can raise them on the module.
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024

MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024

MAX_ARCHIVE_MEMBERS = 100_000

# A tar member's *metadata* -- a PAX record block or a GNU long-name block -- as
# opposed to its contents, which `MAX_EXTRACTED_BYTES` covers. One mebibyte is
# roughly 250 times the longest path any mainstream filesystem accepts, so this
# does not constrain a real archive; what it constrains is a header that declares
# a size no real header would.
MAX_METADATA_BYTES = 1024 * 1024

# And the total across every member. The per-header cap bounds one allocation;
# this bounds the archive. A hundred thousand members each just under the header
# cap is a hundred gigabytes, and `tarfile` retains every parsed `TarInfo` until
# enumeration finishes, so none of it is transient. 64 MiB is far more metadata
# than any real archive carries -- a PAX header exists to hold a long path, and
# 64 MiB is sixteen thousand of them at PATH_MAX.
MAX_TOTAL_METADATA_BYTES = 64 * 1024 * 1024

# The member types whose declared size is metadata to be read into memory rather
# than file contents to be written out. Looked up rather than written as literals
# so a name this `tarfile` does not have is simply absent instead of raising at
# import; `SOLARIS_XHDTYPE` is the one that has moved.
#
# `GNUTYPE_SPARSE` is deliberately *not* here. Its `size` is the file's own data
# length, not a metadata block, so bounding it would refuse a large sparse member
# that extracts perfectly well -- the kind of false positive that comes from
# grouping by "reads something" instead of by what the number means.
_TAR_METADATA_TYPES = frozenset(
    value
    for value in (
        getattr(tarfile, name, None)
        for name in (
            "XHDTYPE",
            "XGLTYPE",
            "SOLARIS_XHDTYPE",
            "GNUTYPE_LONGNAME",
            "GNUTYPE_LONGLINK",
        )
    )
    if value is not None
)

# Whether a fetch may reach a private, loopback or link-local address.
#
# On by default, because the URL comes from a worker response and the caller is
# usually a machine with a metadata service at 169.254.169.254 and services on
# loopback. A worker that can name those can make its client read them.
#
# An operator whose worker legitimately serves from a private network turns this
# off, which is the case the flag exists for -- there is no allowlist because a
# hostname allowlist does not survive rebinding and an address allowlist is what
# the operator's own network already expresses.
ALLOW_PRIVATE_FETCH_TARGETS = False

# Filesystems bound a path component in bytes, and 255 is the limit on ext4,
# APFS and NTFS alike. Checked in bytes rather than characters because the charset
# permitted here includes non-ASCII: 80 CJK characters already exceed this while
# passing any character count.
MAX_OUTPUT_NAME_BYTES = 255

# Mode bits the stdlib `data` tar filter clears, replicated for the fallback path
# on Python patch releases that predate the `filter` parameter.
_UNSAFE_MODE_BITS = 0o7022  # setuid, setgid, sticky, group- and other-writable
