"""Filenames and archive member names: what is refused, and why everywhere."""

from __future__ import annotations

import io
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

from runpod_doc_client import (
    ResponseError,
    extract,
    limits,
    names,
    safe_output_name,
    within,
)
from tests.client_fixtures import (
    _tar_of,
)


@pytest.mark.parametrize("name", ["", ".", "..", "a/b.jpg", "a\\b.jpg", "/abs.jpg"])
def test_an_unusable_output_name_is_refused(name: str) -> None:
    with pytest.raises(ResponseError):
        safe_output_name(name, what="image key")


def test_a_plain_filename_passes() -> None:
    assert safe_output_name("fig_0.jpg", what="image key") == "fig_0.jpg"


def test_within_accepts_the_destination_itself(tmp_path: Path) -> None:
    assert within(tmp_path.resolve(), ".") is True
    assert within(tmp_path.resolve(), "../elsewhere") is False


def test_a_nul_in_an_output_name_is_refused() -> None:
    """It has no directory component and `Path(name).name` keeps it, so every check
    passed — and then the write raised a raw `ValueError: embedded null byte`."""
    with pytest.raises(ResponseError, match="control character"):
        safe_output_name("fig\x00.jpg", what="image key")


@pytest.mark.parametrize("name", ["fig\n.jpg", "fig\t.jpg", "fig\x7f.jpg"])
def test_other_control_characters_are_refused_too(name: str) -> None:
    with pytest.raises(ResponseError, match="control character"):
        safe_output_name(name, what="image key")


def test_within_works_with_a_relative_destination() -> None:
    """The defect here was not a leak but a *wrong answer*: only the target was
    resolved, so a relative destination compared an absolute path against a
    relative one and returned False for every safe member. `extract` passes an
    already-resolved path and so never saw it — a public helper has to be correct
    on its own terms rather than on its caller's."""
    assert within(Path("out"), "doc.md") is True
    assert within(Path("out"), "../escaped") is False


@pytest.mark.parametrize("name", [123, ["a"], {}, 0.5, True])
def test_an_output_name_that_is_not_a_string_is_refused(name: object) -> None:
    """A truthy non-string reached `Path(name)` and raised TypeError. A falsy one
    such as `{}` was caught, but reported as "not a usable filename" — the wrong
    problem, which is its own small defect."""
    with pytest.raises(ResponseError, match="should be a string"):
        safe_output_name(name, what="a basename")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name",
    ["NUL", "CON", "AUX", "PRN", "aux.txt", "COM1", "LPT9", "nul.json", "Con.md"],
)
def test_a_dos_device_name_is_refused(name: str) -> None:
    """On Windows `open("NUL", "wb")` succeeds and discards everything written,
    so an artifact named `NUL` is reported as saved and is gone. Reserved with
    any extension, and refused regardless of the host platform because the client
    writing the file may be on Windows even when the worker was not."""
    with pytest.raises(ResponseError, match="reserved device name"):
        safe_output_name(name, what="a basename")


@pytest.mark.parametrize("name", ["report.", "report ", "a.b.", "x "])
def test_a_trailing_dot_or_space_is_refused(name: str) -> None:
    """Windows strips both silently, so two artifacts one dot apart would collide
    and the second would overwrite the first."""
    with pytest.raises(ResponseError, match="trailing dot or space"):
        safe_output_name(name, what="a basename")


@pytest.mark.parametrize("name", ["CONSOLE.md", "AUXILIARY.txt", "COM.md", "nullable.py"])
def test_names_merely_starting_like_a_device_are_allowed(name: str) -> None:
    """The boundary in the other direction: only the exact stem is reserved, so
    the check must not swallow ordinary filenames."""
    assert safe_output_name(name, what="a basename") == name


@pytest.mark.parametrize("name", ["report?.txt", "a*.png", "x|y.txt", 'q"t.md', "a<b.md", "a>b.md"])
def test_a_windows_forbidden_character_is_refused(name: str) -> None:
    """These cannot be created as files on Windows, and they fail only at write
    time — so a caller trusting the documented "usable filename" result gets an
    OSError instead of a refusal."""
    with pytest.raises(ResponseError, match="Windows filename"):
        safe_output_name(name, what="a basename")


@pytest.mark.parametrize("name", ["fine_name.md", "a-b.c.jpg", "p0001_fig_0.jpg"])
def test_ordinary_names_still_pass(name: str) -> None:
    assert safe_output_name(name, what="a basename") == name


def test_within_refuses_a_nul_in_the_member_name() -> None:
    """Whether `resolve()` raises on a NUL depends on the platform — POSIX
    rejects it, Windows computes a path and answered True — so the same archive
    got two different answers from an exported helper. Checked explicitly for a
    consistent one."""
    with pytest.raises(ResponseError, match="NUL"):
        within(Path("out"), "bad\0name")


def test_within_refuses_a_destination_that_is_not_a_path() -> None:
    """Exported, so a direct caller wraps it in the same `except ResponseError`
    as everything else here; `Path(None)` raised a bare TypeError."""
    with pytest.raises(ResponseError):
        within(None, "x")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "refused"),
    [
        ("a" * 256, True),
        ("a" * 251 + ".jpg", False),
        ("文" * 90, True),    # 270 bytes from 90 characters, refused
        ("文" * 80, False),   # 240 bytes, fits and must not be refused
    ],
)
def test_an_overlong_output_name_is_refused(name: str, refused: bool) -> None:
    """A caller writing this gets `OSError: File name too long`, which is exactly
    the failure this helper exists to turn into a refusal.

    Measured in bytes rather than characters because the permitted charset
    includes non-ASCII: 90 CJK characters is 270 bytes and passes any character
    count, while 80 is 240 and fits.
    """
    if refused:
        with pytest.raises(ResponseError, match="filename limit"):
            safe_output_name(name, what="a basename")
    else:
        assert safe_output_name(name, what="a basename") == name


def test_within_normalises_a_resolution_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On 3.10 and 3.11, a symlink loop encountered while resolving raises
    RuntimeError — not an OSError, so it escaped. A destination reused across
    extractions can contain one, and this function is the code that walks into
    it.

    Forced, because the interpreter running this suite returns the path instead
    of raising.
    """
    original = Path.resolve

    def exploding_resolve(self, *args, **kwargs):
        if "loop" in str(self):
            raise RuntimeError("Symlink loop from " + str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", exploding_resolve)
    with pytest.raises(ResponseError, match="cannot place"):
        within(Path("loop"), "member.md")


def test_an_unpaired_surrogate_in_an_output_name_is_refused() -> None:
    """An unpaired surrogate survives JSON decoding, so a response can carry one.

    The length check measured with errors="surrogatepass" so it could not crash,
    which silently admitted a name that raises UnicodeEncodeError the moment the
    caller writes it, which the narrower check did not cover.
    """
    name = "x" + chr(0xD800) + ".txt"
    with pytest.raises(ResponseError, match="not encodable as UTF-8"):
        safe_output_name(name, what="a basename")


@pytest.mark.parametrize("name", ["ordinary.md", "\u6587\u66f8.md", "a-b_c.jpg"])
def test_encodable_names_still_pass(name: str) -> None:
    """Encoding strictly must not reject legitimate non-ASCII names."""
    assert safe_output_name(name, what="a basename") == name


@pytest.mark.parametrize(
    "name",
    [
        "NUL .txt",          # Windows ignores trailing spaces when matching
        "NUL...txt",
        "NUL.",
        "COM\u00b9.txt",      # superscript one is COM1
        "LPT\u00b2.log",
        "AUX .md",
        "con .json",
    ],
)
def test_every_device_name_spelling_is_refused(name: str) -> None:
    """An exact-stem lookup missed two shapes Windows still treats as devices:
    trailing spaces or dots before the extension, and superscript digits. Both
    were returned as usable and neither can be saved as an ordinary file."""
    with pytest.raises(ResponseError, match="reserved device name"):
        safe_output_name(name, what="a basename")


@pytest.mark.parametrize("name", ["CONSOLE.md", "NULL.md", "COMMS.txt", "AUXILIARY.py"])
def test_names_merely_containing_a_device_name_still_pass(name: str) -> None:
    """The boundary: normalising the stem must not widen the match."""
    assert safe_output_name(name, what="a basename") == name


@pytest.mark.parametrize(
    "name",
    ["document.txt:payload", "NUL", "sub/COM1.txt", "sub/aux.log", "LPT9"],
)
def test_a_windows_special_member_path_is_refused(name: str, tmp_path: Path) -> None:
    """Containment is not enough: `within` says these land under the destination,
    and they do — but tarfile then opens a DOS device or an NTFS alternate data
    stream instead of creating an artifact, so the member is silently discarded
    or hidden while extraction reports success.

    Checked per path component, and on every platform, for the same reason
    `safe_output_name` is: the worker writing the archive and the client
    unpacking it need not share an OS.
    """
    with pytest.raises(ResponseError, match="refusing tar member"):
        extract(_tar_of([name]), tmp_path)


def test_members_windows_would_collapse_together_are_refused(tmp_path: Path) -> None:
    """The P1. Windows rewrites both `a?.txt` and `a*.txt` to `a_.txt`, so an
    archive carrying both has one silently overwrite the other while extraction
    reports success.

    The gap was not the missing characters, it was that `_check_member_name` was
    written as a fresh, narrower copy of a rule `safe_output_name` already had --
    so `a?.txt` was refused as an output name and accepted as a member.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a?.txt", "first")
        archive.writestr("a*.txt", "second")
    with pytest.raises(ResponseError, match="Windows filename"):
        extract(buffer.getvalue(), tmp_path)


@pytest.mark.parametrize(
    "name", ["a?.txt", "NUL", "doc:ads", "trailing.", "trailing ", "a|b.txt"]
)
def test_one_rule_answers_for_both_callers(name: str, tmp_path: Path) -> None:
    """The invariant the fix establishes: a name unusable as an output is also
    unusable as a member. Asserted as agreement rather than as two lists, since
    two lists is exactly what drifted."""
    assert names._windows_component_problem(name) is not None
    with pytest.raises(ResponseError):
        safe_output_name(name, what="a basename")
    with pytest.raises(ResponseError):
        extract(_tar_of([name]), tmp_path)


def test_the_reason_given_is_the_most_specific_one() -> None:
    """`NUL.` is a device name that also ends in a dot; the device reason is the
    useful one, so the checks are ordered most specific first."""
    assert "device name" in (names._windows_component_problem("NUL.") or "")
    assert "trailing dot" in (names._windows_component_problem("report.") or "")


@pytest.mark.parametrize(
    ("first", "second"),
    [("Report.txt", "report.txt"), ("A/B.txt", "a/b.txt"), ("Doc.MD", "doc.md")],
)
def test_members_colliding_under_case_folding_are_refused(
    first: str, second: str, tmp_path: Path
) -> None:
    """Windows and macOS default to case-insensitive, so these are one file there
    and the second extraction silently overwrites the first.

    Every per-name check passes — both names are perfectly legal — because the
    problem is a *relationship* between names. Per-name validation cannot see it
    at all, which is why this needs a pass over the set rather than another rule.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(first, "first")
        archive.writestr(second, "second")
    with pytest.raises(ResponseError, match="same file"):
        extract(buffer.getvalue(), tmp_path)


def test_a_tar_parent_component_is_resolved_rather_than_removed(
    tmp_path: Path
) -> None:
    """The other half of the same rule, which a single canonical form gets wrong.

    In a tar `a/../b.txt` resolves to `b.txt`, so it does *not* collide with
    `a/b.txt`, and reporting that pair would be a false positive. Applying the tar
    rule to both containers would have traded one false negative for one.

    Asserted on the check rather than end to end, and the first version of this did
    the latter -- which passed on Windows and failed on every Linux interpreter.
    `tarfile` does not normalise a member's name, so it tries to create the literal
    parent directory `a/..`: Windows resolves that to the destination and finds it
    already there, while on POSIX `a` does not exist yet, so `makedirs` creates it
    and then fails on the destination. Whether such a member extracts is a
    platform question about `tarfile`; whether it collides is this module's, and
    conflating the two is what made the test wrong.

    The end-to-end call is still made, for the one thing it can say portably: the
    outcome stays inside the error contract either way.
    """
    names._check_member_collisions(["a/../b.txt", "a/b.txt"], container="tar")

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in ("a/../b.txt", "a/b.txt"):
            info = tarfile.TarInfo(name)
            info.size = 0
            tar.addfile(info, io.BytesIO(b""))
    try:
        extract(buffer.getvalue(), tmp_path)
    except ResponseError:
        # POSIX: `tarfile` cannot create `a/..`. Contained, which is the contract.
        return
    assert (tmp_path / "b.txt").is_file()


def test_the_caps_are_reachable_only_through_their_module() -> None:
    """Exporting the numbers made `client.MAX_ARCHIVE_BYTES = bigger` look like the
    documented way to raise a cap while changing nothing -- the readers go through
    `limits`, so the assignment landed on the package and the original number
    stayed in force. Silence in response to following the public surface.

    The module is exported instead, which is the one place an assignment works.
    """
    import runpod_doc_client as package

    assert "limits" in package.__all__
    for name in (
        "MAX_ARCHIVE_BYTES",
        "MAX_EXTRACTED_BYTES",
        "MAX_ARCHIVE_MEMBERS",
        "MAX_METADATA_BYTES",
        "DOWNLOAD_TIMEOUT_SECONDS",
    ):
        assert name not in package.__all__, (
            f"{name} must not be exported as a detached value; assigning it on the "
            f"package would silently do nothing"
        )
        assert hasattr(package.limits, name)


def test_raising_a_cap_on_the_limits_module_takes_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The behaviour the docstring promises, asserted rather than described."""
    monkeypatch.setattr(limits, "MAX_ARCHIVE_MEMBERS", 1)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.txt", "")
        archive.writestr("b.txt", "")
    with pytest.raises(ResponseError, match="over"):
        extract(buffer.getvalue(), Path(tempfile.mkdtemp()))

    monkeypatch.setattr(limits, "MAX_ARCHIVE_MEMBERS", 100)
    destination = Path(tempfile.mkdtemp())
    extract(buffer.getvalue(), destination)
    assert (destination / "a.txt").is_file()


def test_a_symlinked_destination_aliases_two_members(tmp_path: Path) -> None:
    """The lexical check compares names; it cannot see the destination's own links.

    With `a -> b` already present, `a/x.txt` and `b/x.txt` are two names, two
    canonical keys and one file -- so the second replaced the first with every
    lexical rule satisfied.
    """
    (tmp_path / "b").mkdir()
    try:
        (tmp_path / "a").symlink_to(tmp_path / "b", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this platform")
    with pytest.raises(ResponseError, match="same file once the destination"):
        names._check_member_collisions(
            ["a/x.txt", "b/x.txt"], container="zip", destination=tmp_path
        )


def test_an_ordinary_destination_is_unaffected(tmp_path: Path) -> None:
    """The guard: resolving must not refuse two genuinely different members, and a
    destination with no links in it is the normal case."""
    names._check_member_collisions(
        ["a/x.txt", "b/x.txt"], container="zip", destination=tmp_path
    )
