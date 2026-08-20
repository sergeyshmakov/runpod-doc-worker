"""The reusable hub.json checks, run against fixtures rather than a real repo.

The rule with teeth is the length limit: the Hub backend stores every
`description` in a varchar(191) column and rejects the push with an opaque
database error, so a listing that is fine locally fails at publish time.
"""

from __future__ import annotations

import json

import pytest

from runpod_doc_worker import config
from runpod_doc_worker.testing import hub


def _hub(**overrides) -> dict:
    base = {
        "title": "Example Worker",
        "description": "Turns documents into structured data.",
        "type": "serverless",
        "config": {
            "env": [
                {
                    "key": "EXAMPLE_SETTING",
                    "input": {
                        "name": "Example setting",
                        "description": "What this knob does.",
                    },
                },
            ],
        },
    }
    base.update(overrides)
    return base


def _write(tmp_path, data):
    path = tmp_path / "hub.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_clean_hub_json_passes(tmp_path):
    hub.check(_write(tmp_path, _hub()))


def test_long_top_level_description_is_rejected(tmp_path):
    path = _write(tmp_path, _hub(description="x" * 200))
    with pytest.raises(AssertionError, match="top-level description is 200 chars"):
        hub.check(path)


def test_a_description_at_the_limit_passes(tmp_path):
    hub.check(_write(tmp_path, _hub(description="x" * hub.MAX_DESCRIPTION_LENGTH)))


def test_long_env_description_is_rejected(tmp_path):
    data = _hub()
    data["config"]["env"][0]["input"]["description"] = "x" * 200
    with pytest.raises(AssertionError, match="EXAMPLE_SETTING"):
        hub.check(_write(tmp_path, data))


def test_missing_env_name_is_rejected(tmp_path):
    data = _hub()
    del data["config"]["env"][0]["input"]["name"]
    with pytest.raises(AssertionError, match="missing input.name"):
        hub.check(_write(tmp_path, data))


def test_empty_env_description_is_rejected(tmp_path):
    data = _hub()
    data["config"]["env"][0]["input"]["description"] = ""
    with pytest.raises(AssertionError, match="empty description"):
        hub.check(_write(tmp_path, data))


def test_no_env_entries_is_rejected(tmp_path):
    """An empty config.env is more likely a schema regression than intent."""
    data = _hub()
    data["config"]["env"] = []
    with pytest.raises(AssertionError, match="no config.env entries"):
        hub.check(_write(tmp_path, data))


def test_every_problem_is_reported_at_once(tmp_path):
    """One run should name everything wrong, not just the first thing."""
    data = _hub(description="x" * 200)
    data["config"]["env"][0]["input"]["description"] = "y" * 200
    found = hub.problems(data)
    assert len(found) == 2


def test_a_missing_file_says_so(tmp_path):
    with pytest.raises(AssertionError, match="hub.json not found"):
        hub.check(tmp_path / "nope.json")


def test_test_inputs_under_default_roots(tmp_path):
    spec = {"tests": [{"input": {"volume_path": "/runpod-volume/fixture.pdf"}}]}
    path = tmp_path / "tests.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    hub.check_test_inputs(path)


def test_a_worker_declared_root_makes_its_baked_path_valid(tmp_path):
    """A fixture copied into the image lives wherever that image's WORKDIR is.
    The worker declares that root; the harness does not assume it."""
    spec = {"tests": [{"input": {"volume_path": "/worker/test-fixture.pdf"}}]}
    path = tmp_path / "tests.json"
    path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(AssertionError, match="outside the input roots"):
        hub.check_test_inputs(path)

    config.configure(config.WorkerConfig(
        volume_roots=config.DEFAULT_VOLUME_ROOTS + ("/worker",),
    ))
    try:
        hub.check_test_inputs(path)
    finally:
        config.reset()


def test_explicit_roots_override_the_active_config(tmp_path):
    spec = {"tests": [{"input": {"volume_path": "/opt/acme/fixture.pdf"}}]}
    path = tmp_path / "tests.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    hub.check_test_inputs(path, roots=("/opt/acme",))


def test_a_spec_with_no_tests_key_is_rejected(tmp_path):
    """A validator that reports clean on a file that lost its contents is
    worse than no validator."""
    path = tmp_path / "tests.json"
    path.write_text(json.dumps({"version": 1}), encoding="utf-8")
    with pytest.raises(AssertionError, match="no 'tests' key"):
        hub.check_test_inputs(path)


def test_test_input_outside_the_roots_is_rejected(tmp_path):
    spec = {"tests": [{"input": {"volume_path": "/somewhere/else/fixture.pdf"}}]}
    path = tmp_path / "tests.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(AssertionError, match="outside the input roots"):
        hub.check_test_inputs(path)


def test_test_inputs_without_a_volume_path_are_skipped(tmp_path):
    spec = {"tests": [{"input": {"file_url": "https://example.com/a.pdf"}}]}
    path = tmp_path / "tests.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    hub.check_test_inputs(path)
