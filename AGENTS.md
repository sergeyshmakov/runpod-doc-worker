# Agent instructions

## This package has real consumers

`runpod-doc-worker` is a shared Python package used by worker repositories that
pin released tags. Discover current consumers by searching sibling checkouts
and GitHub for package imports and pinned archive URLs; do not put an engine
name in this repository. Pre-1.0 status permits deliberate evolution; it does
not make consumer breakage cheap or implicit.

Treat a change as a package change, not only as a local code edit. A green
package test suite is necessary but is not proof that a pinned worker can
adopt the next release.

## Public compatibility surfaces

The public contract includes more than names exported from `__init__.py`:

- documented functions, classes, parameters, return shapes, and exceptions;
- `WorkerConfig` fields and their defaults;
- operator-facing environment-variable names, prefixes, accepted values,
  precedence, polarity, and unset behavior;
- artifact manifests, input and response shapes, error envelopes, and other
  behavior consumers compose into their handlers;
- installation extras, supported Python versions, and dependency bounds.

Changing a default, renaming a variable, or making a function raise where it
previously returned is a compatibility change. Security hardening can be
breaking too; safer behavior is not automatically a refactor or a patch.

## Two APIs that have each cost a consumer a bug

Both are documented in `transport/net.py`, and both are listed here because the
docstring is not where someone looks before reaching for the obvious name.

- **For any URL that came from a job payload, call `net.check_target`.** It is the
  complete check: shape *and* address policy, in one call. `require_http_url` is
  the shape half alone, and two consumers independently shipped an SSRF by using
  it by itself on a URL they then handed to an engine. `file_url` is the exception
  — `CheckedTargetTransport` applies the policy at connect time, so a worker that
  only fetches through the harness inherits it.
- **`require_http_url` returns the host, not the URL.** It reads like a validator
  that passes its input through. Assigning its result back over the URL replaces
  the whole URL with a bare hostname, which a consumer did.

When adding a check with a partial and a complete form, make the complete one the
obvious name, or say plainly in both docstrings which is which.

## The client half

`runpod_doc_worker.client` is the one subpackage that does not run inside a
worker: it is for code that talks *to* one. It exists because consumers each
carried their own copy of archive extraction, output naming, and payload decoding,
and the copies drifted — a fix in one never reached the identical sites in
another.

Two constraints on it:

- **Standard library only, and no imports from the rest of this package.** A
  client package depends on it to read a response; it must not pull a worker's
  transport stack into an end user's environment. A test asserts that importing it
  loads no httpx, httpcore, boto3, or anyio.
- **One error type.** Everything raises `ResponseError`, so a consumer wraps these
  calls in a single `except` and nothing arrives at user code as a raw stdlib
  exception. A new function that can fail belongs in that contract too.

## Never mark a change breaking on your own

**Do not put `!` in a commit title, and do not write a `BREAKING CHANGE:` footer,
without explicit approval from the maintainer.** Ask first, in plain words, and
let them decide.

This repo runs semantic-release on `main`. A `!` is not a note for readers — it
is an instruction to cut a **major version** and publish it, with no further
gate. An agent that adds one has decided the version number and shipped it. That
matters more here than in a consumer repo, because a major version of this
package is a migration imposed on every worker built on it.

That happened in a consumer repo. A per-job SSRF hardening was committed as
`fix(schema)!:` because it did reverse documented and tested behaviour — a
defensible thing to *propose*. It reached the release branch and published a
**major version** for four bug fixes and one hardening change, and reversing it
meant deleting a published Release and tag and force-pushing the branch. The
opt-in flag that would have made it a patch already existed.

So when a change might break a consumer — a renamed or removed export, a default
that flips, a helper that starts rejecting input it used to accept — **stop and
ask**. Describe what breaks and for whom, and offer the alternatives: ship it as
breaking, put the new behaviour behind an opt-in so nothing breaks, or drop it.
Whether a change is worth a major version is the maintainer's call every time.
The checklist below is how you prepare that question — not a licence to answer it
yourself.

Two related rules, learned the same day:

- Never replay a PR branch's commits onto `origin/main` to reword them. Rebuild
  onto the branch's own base commit, and check
  `git merge-base --is-ancestor <new-head> origin/main` **fails** before pushing.
  Making a PR's commits reachable from `main` is a merge whatever it is called,
  and here a merge is a release.
- `git diff old new` being empty does not make a force-push safe. It says the
  content is unchanged; it says nothing about where the commits now sit.

## Before changing a public contract

1. State the old behavior, proposed behavior, affected consumers, and why the
   benefit is worth the migration cost.
2. Search this repository's code, tests, reference docs, and configuration
   guide for the affected surface.
3. Search known consumer repositories for imports, direct calls, environment
   variables, documented defaults, and behavioral tests. If a consumer
   checkout is unavailable, inspect the remote source and report that its test
   suite was not run.
4. Choose explicitly between preserving compatibility, providing a temporary
   compatibility path, or making a documented breaking change. Ask the
   maintainer before choosing when the tradeoff affects security, behavior, or
   operator policy.
5. Prefer additive worker-owned policy in `WorkerConfig` when different workers
   reasonably need different defaults. Keep the environment variable as the
   operator override and define its precedence precisely.
6. When the operator-facing *name* is itself worker-specific, hand the whole
   knob over rather than only its default. Splitting it — worker picks the
   default, this package owns the variable and its precedence — leaves an
   operator-facing name and polarity being chosen by a dependency, and both
   then move under endpoints already deployed with the old spelling. Filesystem
   diagnostics went that way twice in two releases before the policy moved out
   entirely; do not reinstate a probe default here on the strength of rule 5.

Do not silently rename an operator variable, invert an unset default, add an
exception, or leave a previously meaningful setting inert. When renaming is
worthwhile, consider a deprecated alias and define what happens if old and new
names are both present.

Keep independent contract decisions in separate commits and preferably
separate PRs. A transport fix, configuration migration, default inversion, and
public-API exception each need their own review rationale even if one incident
reveals all of them.

## Compatibility verification

For an affected configuration or API, cover the full behavior matrix that
matters: unset, explicit true, explicit false, legacy name, replacement name,
both names together, custom `env_prefix`, and direct public-function calls.

Before proposing a release:

- run the package suite with `pip install -e ".[test,s3]" && pytest tests/ -v`;
- test the affected known consumer against the candidate package in a
  disposable environment, preferably from the built wheel or source archive
  rather than only by changing `PYTHONPATH`;
- run the consumer tests that express the old contract and update them only
  after the compatibility decision is explicit;
- run documentation lint, typecheck, build, and link validation when public
  behavior changes;
- report exact commands and any checks that could not run.

Do not update a consumer pin merely to make a failing compatibility test go
away. First decide whether the package should preserve the behavior or require
a migration.

## Releases and changelog accuracy

`main` runs semantic-release. Commit classification is therefore a release
decision:

| Change | Required release signal |
| --- | --- |
| Backward-compatible feature | `feat:` (minor) |
| Backward-compatible fix, performance change, or refactor | `fix:`, `perf:`, or `refactor:` (patch) |
| Breaking change while pre-1.0 | `!` and a `BREAKING CHANGE:` footer (minor by `.releaserc.json`) |
| Non-release work | Follow the documented `docs:`, `test:`, `build:`, `ci:`, `chore:`, or `style:` rules |

Every breaking commit must use both a visible `!` marker and a
`BREAKING CHANGE:` footer. The footer must name the old behavior, new behavior,
and who is affected. Include adoption steps only when the maintainer explicitly
requires them. Do not hide a contract break behind a generic subject such as
"tighten boundaries."

Before merge, generate or dry-run the release notes and read them as a
consumer. They must explicitly name every changed environment variable,
default, and exception. Generic CI success does not validate release-note
usefulness.

`CHANGELOG.md` and the version in `pyproject.toml` are owned by semantic-release;
do not edit them by hand for a normal release. Published versions, tags,
release commits, and artifacts are immutable. Correct a bad published release
with a forward release and an explicit correction note; never move, reuse, or
silently rewrite a published version.

## Definition of done

A consumer-affecting change is complete only when the package tests pass, the
affected real consumer has been checked, compatibility behavior is covered by
tests, configuration/reference docs describe the resulting contract, and the
generated release version and notes accurately describe the impact.
