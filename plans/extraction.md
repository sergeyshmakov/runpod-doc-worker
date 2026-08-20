# Harness extraction plan

Extract the engine-agnostic parts of the mineru-runpod worker into this repo,
release v0.1.0, then adopt it back in mineru-runpod. PaddleOCR comes after.

## Position

Extraction happens with one real engine in hand. That is the main risk: an
interface inferred from a single implementation tends to encode that
implementation. Mitigation is Phase 4 — a second, trivial, CPU-only engine
lives in this repo and is exercised by CI from the day the contract exists.
Two implementations, one deliberately unlike the other, is what makes the seam
real. PaddleOCR then becomes the third, not the second.

## Version freeze policy

| Surface | Talks to | Pin | Exposed to API v2? |
|---|---|---|---|
| Worker runtime (handler, stages, bootstrap) | `runpod.serverless.*` | `runpod>=1.7,<2` + guard test | No |
| Ops (create_template, create_endpoint, delete) | control plane via SDK | same pin, isolated adapter | Yes |
| Hub assets (`hub.json`, `tests.json`) | Hub schema | schema validator test | No |

Decisions taken for the duration of this work:

- Build against what SDK 1.x wraps (GraphQL / REST v1). API v2 is beta as of
  2026-07-17; not adopted here.
- Revisit v2 when it leaves beta **and** the Python SDK exposes it. Adopting it
  later is a patch release of `ops/`, not a contract change — neither worker
  repo is touched.
- The upper SDK pin is set from evidence in Phase 5, not from hope.

## Phase 0 — scaffolding

Copy the tooling that already works in mineru-runpod; do not reinvent it.

- `pyproject.toml` — hatchling, package `runpod_doc_worker`, `requires-python
  >=3.10`, deps `runpod>=1.7,<2`, `httpx>=0.27`, `boto3>=1.35`. Extras:
  `test`, `client`, `otel`, `ops`.
- `.github/workflows/ci.yml` — Python 3.11 + 3.12 matrix, mirroring the worker
  repo.
- `commitlint.config.js`, `.releaserc.json`, `package.json` — same conventional
  commit rules and semantic-release setup, `tagFormat: v${version}`.
- `CONTRIBUTING.md`, `SECURITY.md`, `.gitignore`.
- No docs site. README only until v0.1.0.

Exit: `pip install -e ".[test]"` and an empty `pytest` run both succeed on CI.

## Phase 1 — lift the already-generic modules

Move verbatim, mechanical import rewrites only. No behaviour changes, so any
test failure in this phase is a transcription error and nothing else.

| From | To | Notes |
|---|---|---|
| `worker/redact.py` | `obs/redact.py` | zero coupling |
| `worker/logging.py` | `obs/logging.py` | zero coupling |
| `worker/net.py` | `transport/net.py` | env names parameterized in Phase 3 |
| `worker/io.py` | `transport/io.py` | size caps become config |
| `worker/package.py` | `transport/package.py` | manifest work deferred to Phase 2 |
| `worker/debug.py` | `obs/debug.py` | `find_model_dir` takes a hint list |
| `tests/test_redact.py` | as-is | |
| `tests/test_formats_transport.py` | as-is | |
| `tests/test_hub_json.py` | `testing/hub.py` + test | becomes a reusable validator |

Exit: tests green, and a grep test asserts no engine name (`mineru`, `paddle`)
appears anywhere in the package. That grep test stays forever — it is the
tripwire for the failure mode this whole exercise exists to prevent.

## Phase 2 — the contract

`contract/` is new code, written against the MinerU shape as reference but
importing nothing from it.

- `Engine` protocol: `name`, `version`, `available`, `ns`, `env_prefix`,
  `schema()`, `validate(cleaned)`, `run(bytes, work_dir, **opts) -> Path`,
  `artifacts()`, `work_units(cleaned, result)`, `decorate(response, ctx)`,
  `warmup_fixture()`, `metrics()`, `contract` (int).
- `JobCtx` dataclass — job, raw_input, cleaned, file_bytes, input_format,
  work_dir, output_dir, response, phase_ms.
- `Artifact(key, glob, inline)` — the manifest that makes packaging
  data-driven. Kills the hard-coded `.md` / `_content_list.json` /
  `_middle.json` / images layout.
- `CORE_SCHEMA` + merge helper: basename, source XOR, transport, formats,
  archive_format, page ceiling, URL checks. Engine fields arrive as a fragment;
  the validator's known-key set is the union.
- `CONTRACT_VERSION`, checked against `engine.contract` at import so drift is a
  boot error rather than a shape surprise.

Exit: `assert_engine_contract()` exists and passes against a fake engine.

## Phase 3 — envelope and stages

- `contract/stages.py` — validate, fetch_bytes, detect_format, workdir, run,
  package, measure_egress, refresh_check, build_response. Each is
  `async (ctx) -> None`.
- Phase timing, spans and the `phase_duration` histogram become one decorator
  applied to stages, replacing four copies of the same timing block.
- `envelope/handler.py` — `make_handler(pipeline)`, `default_pipeline(engine)`,
  probe mode, the failure envelope, `_measure_output_bytes`.
- `envelope/lifecycle.py` — SIGTERM breadcrumb and drain rationale, cumulative
  refresh counters, concurrency modifier. Env names read
  `{engine.env_prefix}_*`.
- `envelope/bootstrap.py` — the single-asyncio-loop composition (fitness →
  warmup → heartbeat → signals → JobScaler.run → telemetry flush), plus the SDK
  internals guard test moved from the worker repo.
- `obs/telemetry.py` — namespace from `engine.ns`, catalog is
  `CORE_METRICS | engine.metrics()`.

Exit: the response envelope for a fake engine is byte-comparable to the current
worker's for the same input, modulo engine-named keys.

## Phase 4 — reference engine

`examples/echo_engine/` — a CPU-only text extractor (~60 lines over
`pypdfium2`) implementing `Engine` end to end, plus a Dockerfile that builds
without CUDA.

Purpose, in order of importance:

1. Second implementation, so the contract is not a MinerU tracing.
2. CI runs a full job end to end — fetch, detect, run, package, respond — on a
   GitHub runner with no GPU.
3. Copy-paste starting point for both real workers, and the thing the README
   shows.

Exit: `assert_engine_contract(EchoEngine())` green; CI runs a real
`handler(job)` through all three transports.

## Phase 5 — ops, isolated

- `ops/control_plane.py` — protocol: create_template, create_endpoint,
  update_endpoint_template, delete_endpoint, delete_template.
- `ops/sdk_v1.py` — the only implementation for now, wrapping the same SDK calls
  deploy.py and destroy.py already make.
- `ops/cli.py` — console scripts `runpod-doc-deploy` / `runpod-doc-destroy`,
  argparse lifted from deploy.py with engine-specific defaults injected.
- **SDK matrix task**: run the internals guard against runpod 1.9, 1.10, 1.11
  and 1.12, and record which still satisfy the JobScaler / Heartbeat /
  run_fitness_checks shape. Set the upper pin from that result.
- **Early-warning CI job**: weekly schedule, installs the latest `runpod`, runs
  only the internals guard and an ops import smoke, `continue-on-error: true`.
  Never blocks a PR. This is how we learn the newbie bugs are fixed without
  being forced to move.
- `docs/runpod-api-versions.md` — records the freeze decision, the v2 beta date,
  and the conditions for revisiting.

Exit: both console scripts deploy and tear down a real endpoint.

## Phase 6 — client core (optional for v0.1.0)

`client/` under the `[client]` extra: safe tar/zip extraction with traversal
guards, output-name sanitizing, presigned download, poll loop. Not installed in
worker images. Ships only if it does not delay the release.

## Phase 7 — release and adoption

1. Tag `v0.1.0` from this repo's semantic-release.
2. In mineru-runpod: add the pinned tarball to `requirements.txt`, delete the
   moved modules, reduce `handler.py` to a `MineruEngine` plus the pipeline
   assembly, leave `mineru_client/` alone.
3. Capture a golden response JSON from the current worker **before** the swap
   (all three transports, one real document). Acceptance is a byte-identical
   diff afterwards, except engine-named keys.
4. Per pre-1.0 convention, `mineru_version` is dropped outright in favour of
   engine info — no alias, no mirror.
5. Commits land as `refactor:`. No `!`, no `BREAKING CHANGE:` footer.
6. Live verification: deploy, one cold start, one warm parse, one S3 job, one
   probe job.

## Not in scope for v0.1.0

- API v2 adapter.
- The MinerU v4 API emulation — no analogue in a second engine, stays in the
  worker repo.
- Docs site, Hub listing for this repo.
- Any PaddleOCR code.

## Local layout

Three sibling checkouts. The dev loop is `uv pip install -e ../runpod-doc-worker`
from whichever worker repo is being worked on.
