# Contributing to runpod-doc-worker

Thanks for considering a contribution. This package is the engine-agnostic half of a RunPod document-processing worker — the parts that would otherwise be copy-pasted between workers that run different models.

## What this repo is responsible for

- Input transport: fetching bytes from a URL, a base64 payload or a network volume, and detecting the format
- Outbound target checks: refusing a URL that resolves somewhere the worker should not connect
- Response packaging: tarball, inline and S3 transports, and the artifact manifest that describes an engine's output
- Structured logging, failure-text redaction, GPU and filesystem debug probes
- Checks worker repos reuse in their own suites (`runpod_doc_worker.testing`)

## What this repo is *not* responsible for

- **Anything a specific engine does** — parsing quality, model selection, backend flags. That belongs in the worker repo that owns the engine.
- **RunPod platform behaviour** (endpoints not scaling, cold starts, FlashBoot) → RunPod support.
- **The wire contract of any particular worker.** This package supplies the pieces; each worker composes them and owns what its callers see.

A useful test for whether something belongs here: would a worker running a completely different model still want it, unchanged? If not, it goes in the worker repo.

## The rule that keeps this package honest

No engine name appears in `runpod_doc_worker/`. Not in code, not in a docstring, not in an error message. Anything engine-specific arrives as data — through `WorkerConfig` or an artifact manifest. `tests/test_no_engine_names.py` enforces this; if it fails, the fix is a new config field, not an exception to the rule.

## Bug reports

Please include:

- The `WorkerConfig` the worker installed (env prefix, model globs)
- The artifact manifest, if the problem is in packaging
- The full failure text, including the `error` field if a job returned one
- Which engine the worker runs, so we can tell a harness bug from an engine one

## Pull requests

- Keep PRs small and focused. Independent bug fix + refactor = two PRs.
- Run the suite locally: `pip install -e ".[test,s3]" && pytest -v`.
- New code paths need at least one test. CPU-only — CI has no GPU, and nothing here should need one.
- `CHANGELOG.md` is maintained by semantic-release — do **not** edit it by hand. Write a good commit message instead.

## Commit message format — Conventional Commits

We use [Conventional Commits](https://www.conventionalcommits.org/) so version bumps and changelog entries are generated automatically.

```
type(optional-scope): short summary in present tense

optional body explaining the why, wrapped at ~80 cols
```

Types that **trigger a release**:

| Type | Bump | Example |
|---|---|---|
| `feat:` | minor | `feat(transport): add a zip archive container` |
| `fix:` | patch | `fix(net): keep the caller's host in the Host header` |
| `perf:` | patch | `perf(package): stream the archive instead of buffering` |
| `refactor:` | patch | `refactor(obs): read the logger name from config` |
| `revert:` | patch | `revert: revert "feat: zip archive container"` |

Types that **do not** trigger a release by themselves: `docs:` (except `docs(readme):`, a patch), `test:`, `build:`, `ci:`, `chore:`, `style:`. Any type marked with `!` and a `BREAKING CHANGE:` footer triggers the breaking-change rule below.

**Breaking changes must use `!` and a `BREAKING CHANGE:` footer.** While the package is pre-1.0, `.releaserc.json` deliberately maps a breaking change to a **minor** bump rather than the default major bump. The footer must name the old behavior, the new behavior, and affected consumers; include adoption steps only when the maintainer requires them. Do not classify a renamed environment variable, inverted default, new public exception, or other compatibility break as an ordinary `refactor:` patch.

Commitlint runs on every PR. Preview locally with `npx commitlint --from HEAD~1 --to HEAD --verbose`.

## Stability

Pre-1.0, but already used by worker repositories that pin release tags. Breaking changes require an explicit maintainer decision, a minor release, downstream-consumer verification, accurate release notes, and current contract documentation. Prefer an additive or temporary compatibility path when it materially reduces operational risk; do not assume a pinned dependency makes an undocumented break harmless.

## Code style

No enforced formatter — match the surrounding code. Comments explain why, not what. Type hints encouraged, not required.

## License

By contributing, you agree your contributions are licensed under the [MIT License](LICENSE) of this repo.
