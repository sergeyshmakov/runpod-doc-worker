"""The names consumers import, asserted so a refactor cannot quietly remove one.

This exists because a refactor did. Splitting `obs/debug.py` moved the model-cache
functions into their own module and left re-export imports behind -- and then
`ruff --fix F401` removed the ones `debug.py` did not itself call, because an
unused import is indistinguishable from a deliberate re-export unless the module
says so. `find_model_dir` is named in the README and the reference docs, and it
stopped existing.

Nothing in the repository noticed. The consumer that imports it did, several
commits later, which is the wrong place to find out.

The lists below are the documented surface. Adding a name here is cheap; the point
is that removing one has to be deliberate.
"""

from __future__ import annotations

import importlib

import pytest

# Module path -> the names it must keep exporting. Sourced from README.md and
# docs/content/docs/reference/index.mdx, which are what a consumer reads.
PUBLIC = {
    "runpod_doc_worker.obs.debug": (
        "collect_gpu_info",
        "find_model_dir",
        "probe_filesystem",
    ),
    "runpod_doc_worker.obs.logging": (
        "debug",
        "error",
        "info",
        "job_id_var",
        "warning",
    ),
    "runpod_doc_worker.obs.redact": ("compact", "compact_url"),
    "runpod_doc_worker.contract.artifacts": (
        "Artifact",
        "ArtifactError",
        "check_basename",
        "keys",
        "resolve",
        "validate",
    ),
    "runpod_doc_client": (
        "ResponseError",
        "decode_b64",
        "download",
        "extract",
        "require_fetchable_url",
        "safe_output_name",
        "within",
    ),
    "runpod_doc_worker.testing.hub": ("check", "check_test_inputs", "problems"),
}


@pytest.mark.parametrize(
    ("module_path", "name"),
    [(path, name) for path, names in PUBLIC.items() for name in names],
)
def test_a_documented_name_is_still_exported(module_path: str, name: str) -> None:
    module = importlib.import_module(module_path)
    assert hasattr(module, name), (
        f"{module_path}.{name} is documented but no longer exists. If it moved, "
        f"re-export it and add the name to that module's __all__ so the import "
        f"does not read as unused."
    )


def test_the_reexports_are_declared_rather_than_incidental() -> None:
    """A re-export needs `__all__` to survive an unused-import autofix.

    Checked for the two modules that re-export from a submodule after being split.
    Without the declaration the names live only in an import statement, which every
    linter is entitled to delete -- and one did.
    """
    for module_path in ("runpod_doc_worker.obs.debug",):
        module = importlib.import_module(module_path)
        declared = getattr(module, "__all__", None)
        assert declared, f"{module_path} re-exports names and declares no __all__"
        for name in PUBLIC[module_path]:
            assert name in declared, f"{name} is not in {module_path}.__all__"
