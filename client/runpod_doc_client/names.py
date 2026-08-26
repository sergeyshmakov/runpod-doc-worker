"""What a filename or an archive member may be called, and what it resolves to.

Every rule here is applied on every platform, deliberately. The worker that wrote
an archive and the client unpacking it need not share an operating system, so a
name that only Windows mangles still has to be refused on Linux -- otherwise the
same response is safe or unsafe depending on who reads it.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from runpod_doc_client import limits
from runpod_doc_client.errors import ResponseError

# Reserved on Windows with any extension, and `open()` on one succeeds while
# discarding the data.
_DOS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{digit}" for digit in "123456789"}
    | {f"LPT{digit}" for digit in "123456789"}
)


# Characters Windows refuses in a filename. `:` and the separators are already
# caught by the plain-filename check; these are the rest, and they fail only when
# the file is created — so a caller trusting this helper's "usable filename"
# result gets an OSError at write time instead of a refusal here.
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')


def _device_stem(name: str) -> str:
    """The stem Windows compares against its reserved device names.

    Not simply ``name.split(".")[0].upper()``. Windows ignores trailing spaces and
    dots when matching, so ``"NUL .txt"`` is the ``NUL`` device, and it treats the
    superscript digits as their ASCII equivalents, so ``"COM\u00b9.txt"`` is
    ``COM1``. Both passed an exact-stem lookup and neither can be saved as an
    ordinary file.
    """
    stem = name.split(".")[0]
    for superscript, digit in (("\u00b9", "1"), ("\u00b2", "2"), ("\u00b3", "3")):
        stem = stem.replace(superscript, digit)
    return stem.rstrip(" .").upper()


def _windows_component_problem(part: str) -> str | None:
    """Why Windows cannot store a path component under this exact name, or None.

    One rule with two callers. ``safe_output_name`` had the full set and
    ``_check_member_name`` was written as a fresh, narrower copy that knew only
    about device names and colons, so ``a?.txt`` was refused as an output name and
    accepted as an archive member. That gap is not cosmetic: Windows silently
    rewrites both ``a?.txt`` and ``a*.txt`` to ``a_.txt``, so an archive carrying
    both has one overwrite the other while extraction reports success.

    Duplicating a rule and weakening the copy is the failure here, not the
    characters that were missing from it.

    Ordered most specific first, so the reason given is the useful one: ``NUL.``
    is reported as a device name rather than as a trailing dot.
    """
    if any(character < chr(32) or character == chr(127) for character in part):
        return "contains a control character"
    if _device_stem(part) in _DOS_DEVICE_NAMES:
        return "is a reserved device name on Windows"
    if ":" in part:
        return "is alternate-data-stream syntax on Windows"
    forbidden = sorted(_WINDOWS_FORBIDDEN.intersection(part))
    if forbidden:
        return (
            "contains " + repr("".join(forbidden))
            + ", which cannot appear in a Windows filename"
        )
    if part[-1] in " .":
        return "has a trailing dot or space, which Windows strips"
    return None


def _extractor_path(name: str, *, container: str) -> str:
    """The path the extractor writes, with case preserved.

    The same component handling as :func:`_canonical_member` -- zip removes `..`,
    tar resolves it -- but without the case folding, because this one is used to
    build a real path rather than a comparison key.
    """
    parts: list[str] = []
    for part in name.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if container == "tar" and parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _canonical_member(name: str, *, container: str) -> str:
    """The path the extractor will actually write, as a comparison key.

    Container-specific, because the two containers resolve parent components
    differently and one rule is wrong for one of them:

    * zip -- ``zipfile`` *removes* ``..`` while extracting, so ``a/../b.txt``
      lands at ``a/b.txt``;
    * tar -- extraction lets the filesystem resolve it, so the same member lands
      at ``b.txt``.

    A zip-shaped rule applied to both meant a tar carrying ``a/../b.txt`` and
    ``b.txt`` compared ``a/b.txt`` against ``b.txt``, found no collision, and let
    the second overwrite the first. Folding answers "same name"; this has to
    answer "same file", and that depends on who does the extracting.
    """
    parts: list[str] = []
    for part in name.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if container == "tar" and parts:
                parts.pop()
            continue
        parts.append(part)
    # Normalised before folding. macOS is normalisation-insensitive as well as
    # case-insensitive, so NFC `\u00e9` and NFD `e` + combining acute are one file
    # there -- and `casefold` leaves the two strings distinct, so the archive
    # passed this check and the second member overwrote the first. Folded again
    # afterwards, then re-normalised, because case folding can itself denormalise
    # its result; that is the composition the Unicode caseless-matching rule
    # specifies rather than something invented here.
    joined = unicodedata.normalize("NFC", "/".join(parts))
    return unicodedata.normalize("NFC", joined.casefold())


def _check_member_collisions(
    names: list[str], *, container: str, destination: Path | None = None
) -> None:
    """Refuse an archive whose member paths collide on a case-insensitive volume.

    Windows and macOS default to case-insensitive, so ``Report.txt`` and
    ``report.txt`` are one file there. Each name passes every per-name check --
    they are both perfectly legal -- and then the second extraction overwrites the
    first, silently, while the archive reports two members and extraction reports
    success.

    That is the same shape as the earlier collision finding, where Windows itself
    rewrote two distinct names into one. Per-name validation cannot see either:
    the problem is a relationship between names, so it needs a pass over the set.
    """
    seen: dict[str, str] = {}
    for name in names:
        key = _canonical_member(name, container=container)
        if not key:
            continue
        first = seen.get(key)
        if first is not None:
            # Including when the two names are byte-identical. An earlier version
            # exempted that case, reasoning that a duplicate name is legal in a
            # zip and that refusing it would reject archives which extract. Both
            # halves are true and the conclusion was still wrong: extraction
            # writes one file, so the first member's payload is gone, and this
            # module's whole contract is that a response does not lose data
            # quietly. "Legal" and "lossless" are different questions.
            detail = (
                f"member {name!r} twice"
                if first == name
                else f"members {first!r} and {name!r}"
            )
            raise ResponseError(
                f"refusing {container} {detail}: they resolve to the same file"
            )
        seen[key] = name

    if destination is not None:
        _check_destination_collisions(names, container=container, destination=destination)


def _check_destination_collisions(
    names: list[str], *, container: str, destination: Path
) -> None:
    """Refuse members that land on one file because the *destination* aliases them.

    The lexical check above compares the names an archive carries. It cannot see a
    symlink that already exists where the files are going: with `a -> b` in the
    destination, `a/x.txt` and `b/x.txt` are two distinct names, two distinct
    canonical keys, and one file -- so the second silently replaced the first with
    every lexical rule satisfied.

    Resolved rather than refused outright, because a symlinked output directory is
    a normal thing for a caller to arrange and refusing all of them would break
    working setups. What is refused is two members resolving to one path.

    `resolve()` on a path that does not exist yet still resolves the parts that
    do, which is exactly the aliasing this needs to see.
    """
    landed: dict[Path, str] = {}
    for name in names:
        # Normalised the way the extractor will, before resolving. Resolving the
        # raw name let `a/../x.txt` collapse to `destination/x.txt` -- while
        # `zipfile` drops the `..` and writes `a/x.txt`, which follows a symlinked
        # `a` to somewhere else entirely. Two members then landed on one file with
        # this check finding nothing, because it had resolved a path the extractor
        # never uses.
        target = destination / _extractor_path(name, container=container)
        try:
            resolved = target.resolve()
        except (OSError, ValueError, RuntimeError) as e:
            # ValueError: a PAX header can carry an embedded NUL, and `resolve()`
            # raises on it -- from a pass that runs before the name checks, so the
            # raw stdlib exception escaped `extract()` and broke the one-error
            # contract. RuntimeError is the symlink-loop spelling on some versions.
            raise ResponseError(
                f"refusing {container} member {name!r}: its destination cannot be "
                f"resolved: {e}"
            ) from e
        first = landed.get(resolved)
        if first is not None and first != name:
            raise ResponseError(
                f"refusing {container} members {first!r} and {name!r}: they land "
                f"on the same file once the destination's own links are resolved"
            )
        landed[resolved] = name


def _check_member_name(name: str, *, container: str) -> None:
    """Refuse an archive member whose path Windows would not store faithfully.

    Containment is not enough. `within` answers "does this land under the
    destination", and a member called `NUL`, `document.txt:payload` or `a?.txt`
    does -- but the filesystem then opens a device, creates an alternate data
    stream, or silently renames it, so the member is discarded, hidden, or
    collides with another while extraction reports success.

    Checked per component and on every platform, for the same reason
    `safe_output_name` is: the worker writing the archive and the client unpacking
    it need not share an OS.
    """
    for part in name.replace("\\", "/").split("/"):
        if not part or part in (".", ".."):
            continue
        problem = _windows_component_problem(part)
        if problem is not None:
            raise ResponseError(
                f"refusing {container} member {name!r}: {part!r} {problem}"
            )


def within(destination: Path, name: str) -> bool:
    """Whether archive member ``name`` lands inside ``destination``.

    Both sides are resolved. Only the target used to be, so a *relative*
    destination — which is the obvious way to call an exported function — compared
    an absolute path against a relative one and returned False for every safe
    member. ``extract`` passes an already-resolved path and so never saw it, which
    is exactly why a public helper has to be correct on its own terms rather than
    on its caller's.
    """
    if isinstance(name, str) and "\x00" in name:
        # Checked explicitly because whether `resolve()` raises on a NUL depends
        # on the platform: POSIX rejects it, Windows computes a path and returns
        # True, so the same archive got two different answers. No valid member
        # name contains one, and a member name is untrusted by definition.
        raise ResponseError(f"refusing member {name!r}: contains a NUL")
    try:
        base = Path(destination).resolve()
        target = (base / name).resolve()
    except (TypeError, ValueError, OSError, RuntimeError) as e:
        # Exported, so a direct caller wraps it in the same `except ResponseError`
        # as everything else here. A NUL in either argument raises ValueError and a
        # non-path destination raises TypeError, both from `Path` rather than from
        # this function — and a member name is untrusted input by definition, which
        # is the whole reason this check exists.
        #
        # RuntimeError is the 3.10/3.11 spelling for a symlink loop encountered
        # while resolving. A destination reused across extractions can contain
        # one, and this function is exactly the code that walks into it.
        raise ResponseError(f"cannot place {name!r} in {destination!r}: {e}") from e
    return target == base or base in target.parents


def safe_output_name(name: str, *, what: str) -> str:
    """Return ``name`` if it is usable as a single output filename.

    Result dicts name the files a client writes — an entry's ``basename`` becomes
    a document stem, and each key of an image map becomes a file in a directory.
    Both are only ever plain filenames coming from a worker, so anything carrying
    a directory component means the caller is holding a result this code did not
    produce, and guessing what they meant is worse than saying so.
    """
    # A parsed response honours its annotation only if the worker sent what it
    # promised. A truthy non-string — `123`, `["a"]` — passed the emptiness check
    # and then made `Path(name)` raise a bare TypeError; a falsy one such as `{}`
    # was caught, but reported as "not a usable filename", which describes the
    # wrong problem.
    if not isinstance(name, str):
        raise ResponseError(
            f"{what} should be a string; got {type(name).__name__}"
        )
    if not name or name in (".", ".."):
        raise ResponseError(f"refusing {what} {name!r}: not a usable filename")
    if name != Path(name).name or "/" in name or "\\" in name:
        raise ResponseError(f"refusing {what} {name!r}: expected a plain filename")
    # A NUL passes every check above — it has no directory component and
    # `Path(name).name` keeps it — and then every file API rejects it with a
    # raw `ValueError: embedded null byte`. Control characters are the same
    # shape of problem: nothing here would stop them and nothing downstream
    # wants them in a filename.
    if any(character < " " or character == "\x7f" for character in name):
        raise ResponseError(
            f"refusing {what} {name!r}: contains a control character"
        )
    # A DOS device name is not a file. On Windows `open("NUL", "wb")` succeeds and
    # discards everything written to it, so an artifact named `NUL` would be
    # reported as saved and be gone — the quietest possible data loss. The names
    # are reserved with any extension (`aux.txt`) and the behaviour does not
    # depend on this process running on Windows, because the caller writing the
    # file might be.
    problem = _windows_component_problem(name)
    if problem is not None:
        raise ResponseError(f"refusing {what} {name!r}: {problem}")
    try:
        encoded = len(name.encode("utf-8"))
    except UnicodeEncodeError as e:
        # An unpaired surrogate survives JSON decoding, so a response can carry
        # a name containing one. This measured length with errors="surrogatepass"
        # so the check itself could not crash -- which silently admitted a name
        # that raises UnicodeEncodeError the moment the caller writes it on a
        # UTF-8 filesystem. Encoding strictly makes measuring and accepting the
        # same question, which is what it should have been.
        raise ResponseError(
            f"refusing {what} {name!r}: not encodable as UTF-8 ({e.reason})"
        ) from e
    if encoded > limits.MAX_OUTPUT_NAME_BYTES:
        # A caller writing this gets `OSError: [Errno 36] File name too long`,
        # which is the failure this helper exists to turn into a refusal — it
        # validates response-provided names *so that* the write is safe.
        raise ResponseError(
            f"refusing {what}: {encoded} bytes exceeds the "
            f"{limits.MAX_OUTPUT_NAME_BYTES}-byte filename limit"
        )
    if _device_stem(name) in _DOS_DEVICE_NAMES:
        raise ResponseError(
            f"refusing {what} {name!r}: reserved device name on Windows"
        )
    # Windows silently strips these, so `report.` becomes `report` and two
    # artifacts one dot apart would collide and overwrite each other.
    if name[-1] in " .":
        raise ResponseError(
            f"refusing {what} {name!r}: trailing dot or space"
        )
    return name
