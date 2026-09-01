## [0.9.0](https://github.com/sergeyshmakov/runpod-doc-worker/compare/v0.8.1...v0.9.0) (2026-09-01)

### ⚠ BREAKING CHANGES

* **logging:** the JSON log record's message field is renamed from `msg` to
`message`. A dashboard, alert or saved query selecting on `msg` will stop
matching and must be updated. The text format is unchanged, as is every
call site. Approved by the maintainer before committing, per AGENTS.md.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>

* docs(observability): describe the record shape the emitters now write

The guide listed `msg` among the fields a caller cannot replace, which was the
old key and is now the parameter name instead. It also did not say what the
record contains, so the one thing worth knowing -- that the text is under
`message` because that is what RunPod's viewer reads -- had to be discovered
from the source.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>

* docs(logging): correct the record shape in the _format_json docstring

The one-liner still said "Always includes ts, level, logger, msg" while the line
below it emitted `message`. A docstring describing a record shape is the thing a
maintainer reads instead of the dict literal, so a stale one sends the next
reader to build against a field that is no longer written.

Caught in review on the same PR that renamed the key.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>

### Bug Fixes

* **logging:** name the JSON message field `message`, as RunPod reads it ([#15](https://github.com/sergeyshmakov/runpod-doc-worker/issues/15)) ([0678193](https://github.com/sergeyshmakov/runpod-doc-worker/commit/0678193357d440224d5738d1ec6ca57a77caa77b))

## [0.8.1](https://github.com/sergeyshmakov/runpod-doc-worker/compare/v0.8.0...v0.8.1) (2026-09-01)

### Bug Fixes

* **coerce:** close the last two routes out of the rejection envelope ([#14](https://github.com/sergeyshmakov/runpod-doc-worker/issues/14)) ([f236a2a](https://github.com/sergeyshmakov/runpod-doc-worker/commit/f236a2ae39572fa42ab593e27b18c5bd5a654fe9))

## [0.8.0](https://github.com/sergeyshmakov/runpod-doc-worker/compare/v0.7.2...v0.8.0) (2026-08-31)

### Features

* share the metric catalog and the single-value input validators ([#13](https://github.com/sergeyshmakov/runpod-doc-worker/issues/13)) ([5b900c8](https://github.com/sergeyshmakov/runpod-doc-worker/commit/5b900c8932818834399b86ffb974d08604a0b657))

## [0.7.2](https://github.com/sergeyshmakov/runpod-doc-worker/compare/v0.7.1...v0.7.2) (2026-08-27)

### Bug Fixes

* **net:** let a caller-supplied URL refuse the local-fetch bypass ([e106c3f](https://github.com/sergeyshmakov/runpod-doc-worker/commit/e106c3f2e6011f9e20f37f3569ced95c142ebdb1))
* **net:** tell an operator something they can act on, and stop calling file_url operator-owned ([a609974](https://github.com/sergeyshmakov/runpod-doc-worker/commit/a609974bb4a98e0bba0c50a4241a4c08066a8ba5))

## [0.7.1](https://github.com/sergeyshmakov/runpod-doc-worker/compare/v0.7.0...v0.7.1) (2026-08-27)

### Bug Fixes

* **release:** write the version into both distributions, not just the root ([50d5e31](https://github.com/sergeyshmakov/runpod-doc-worker/commit/50d5e316892da5517b39828e03fedb977d3e0001))

## [0.7.0](https://github.com/sergeyshmakov/runpod-doc-worker/compare/v0.6.0...v0.7.0) (2026-08-27)

### Features

* **transport:** make the cap refusal opt-in, and measure a whole response ([c6c485c](https://github.com/sergeyshmakov/runpod-doc-worker/commit/c6c485c36b8ba739e4591db5c784d11a95b763bc))
* **transport:** refuse a response the gateway would silently discard ([152cc00](https://github.com/sergeyshmakov/runpod-doc-worker/commit/152cc00485b5c918f14aa1879dd1974f81f0c5d0))

### Bug Fixes

* **transport:** actually call the cap check, and fix two gaps in its messages ([aa770c5](https://github.com/sergeyshmakov/runpod-doc-worker/commit/aa770c5e263e94b01b275f7dc4ff2bde168fe044))

## [0.6.0](https://github.com/sergeyshmakov/runpod-doc-worker/compare/v0.5.0...v0.6.0) (2026-08-26)

### Features

* **client:** require 3.10.12 and delete the data-filter emulation ([c44f8bb](https://github.com/sergeyshmakov/runpod-doc-worker/commit/c44f8bba2d9c4893ddb4bb9412ca4f74c3d189c3))
* **client:** share the response-reading half with consumers ([1dca841](https://github.com/sergeyshmakov/runpod-doc-worker/commit/1dca841e6fd0c6700a20748487d97d9a0aac5ab5))
* **client:** ship the client half as its own distribution ([ac92805](https://github.com/sergeyshmakov/runpod-doc-worker/commit/ac92805d814a035b880cccda82677e990c9ede82))

### Bug Fixes

* **client:** apply the quotas to tar, check member paths, keep setgid ([2730aba](https://github.com/sergeyshmakov/runpod-doc-worker/commit/2730aba8e129d7f061f0c233c44edd9f1cd339aa))
* **client:** bound tar detection, shut the socket down, normalise names ([aca7f12](https://github.com/sergeyshmakov/runpod-doc-worker/commit/aca7f1257865362a8604d62c6070a818e790612d))
* **client:** bound tar member metadata before it is read ([3891a83](https://github.com/sergeyshmakov/runpod-doc-worker/commit/3891a83270f7499c519bd598cca27d84211fcd5b))
* **client:** canonical member paths, prepended data, a deadline that bounds ([9a19afe](https://github.com/sergeyshmakov/runpod-doc-worker/commit/9a19afefa6fa041c58b376d4b92769a846b947e3))
* **client:** case collisions, a counted directory, and zstd errors ([306e193](https://github.com/sergeyshmakov/runpod-doc-worker/commit/306e193b9df919d531ae692f401165dce391e510))
* **client:** charge metadata before the constructor, and four more boundary gaps ([74540c0](https://github.com/sergeyshmakov/runpod-doc-worker/commit/74540c05e8ad21b42cbda751d6e9a544feee706f))
* **client:** check every redirect hop, and four smaller gaps ([1adb549](https://github.com/sergeyshmakov/runpod-doc-worker/commit/1adb54940d6d83fd3f25bd599e31f844421d0662))
* **client:** close five leaks in the error boundary, and correct one claim ([b30dd48](https://github.com/sergeyshmakov/runpod-doc-worker/commit/b30dd48f8cf991738f77716f556bb9deb609677f))
* **client:** close four more boundary gaps, one of them a wrong answer ([756b49e](https://github.com/sergeyshmakov/runpod-doc-worker/commit/756b49e9f9bfef0799d12f6674f2dc519a508534))
* **client:** close the boundary class, not the four reported instances ([37a010e](https://github.com/sergeyshmakov/runpod-doc-worker/commit/37a010e7500630a2a9571218014d3ac7f9bed630))
* **client:** container-aware collisions, ZIP64 stubs, header-phase cancellation ([f3c8e4d](https://github.com/sergeyshmakov/runpod-doc-worker/commit/f3c8e4d6491b53e48ccc14f87378ffba1f223b18))
* **client:** detect the outer container, and bound what a response can cost ([679c293](https://github.com/sergeyshmakov/runpod-doc-worker/commit/679c2938e88bd07944ccfd82b999215d38d6a434))
* **client:** judge the requested origin when a proxy carries the request ([c8052af](https://github.com/sergeyshmakov/runpod-doc-worker/commit/c8052af04e4f21c908291f494f051ee6c44d4cee))
* **client:** make the isolation promise real, and close three more type gaps ([ebc047f](https://github.com/sergeyshmakov/runpod-doc-worker/commit/ebc047f1ab8f42a6b504e4318240f0b8b75abfd6))
* **client:** more zip constructor failures, and strict name encoding ([b4e565a](https://github.com/sergeyshmakov/runpod-doc-worker/commit/b4e565a55c128027e0567f8af27d8b8bb2f3d4eb))
* **client:** one filename rule, incremental quotas, a download deadline ([77ba730](https://github.com/sergeyshmakov/runpod-doc-worker/commit/77ba730b9bd14e41e58fcf20582b5cc47ce3e9e4))
* **client:** parent components, ZIP64 offsets, and real cancellation ([2168780](https://github.com/sergeyshmakov/runpod-doc-worker/commit/216878010b1b18f2230c2631af13854c2cb86bd7))
* **client:** proxied IPv6 origins, symlink aliasing, and a live probe control point ([60ae2ff](https://github.com/sergeyshmakov/runpod-doc-worker/commit/60ae2ffbfa11a6efef04bc496214cbdfe8609463))
* **client:** rename the URL helper, and guard destination resolution ([c122dd5](https://github.com/sergeyshmakov/runpod-doc-worker/commit/c122dd5baffebca0e76f612e6d6c99134ce7e2de))
* **client:** repair two tests that were not testing anything, and three defects ([861e4ae](https://github.com/sergeyshmakov/runpod-doc-worker/commit/861e4aea51c1a5690b325a406548b8f5f1f6ecdc))
* **client:** routability policy, cumulative metadata, duplicate names, cap access ([54d98c6](https://github.com/sergeyshmakov/runpod-doc-worker/commit/54d98c61a080d62c5e95db624335c88c805fc663))
* **client:** start the deadline before open, preflight the zip entry count ([c4454cb](https://github.com/sergeyshmakov/runpod-doc-worker/commit/c4454cbfb0c1ba5fc24f178e43d2cf40eea7b6e3))
* **client:** the legacy tar fallback needs a real mode, not None ([200cb17](https://github.com/sergeyshmakov/runpod-doc-worker/commit/200cb17c66fefd23030b280de1ec256d0bc6d9fe))
* **client:** transcribe the data filter's mode rules, and catch timestamp errors ([8a49c3a](https://github.com/sergeyshmakov/runpod-doc-worker/commit/8a49c3a68615de9b4f5e25300c083aa6d5366aa7))
* **client:** validate the whole authority, and finish the legacy tar fallback ([6036e25](https://github.com/sergeyshmakov/runpod-doc-worker/commit/6036e2545664564d8fdcc4f784b132220015c931))
* **client:** zip name decoding, symlink loops, and name length ([c8ced3e](https://github.com/sergeyshmakov/runpod-doc-worker/commit/c8ced3e821be23874ba300061f1fec6961f841c4))
* **obs:** restore the re-exports the split dropped, and assert the surface ([148156e](https://github.com/sergeyshmakov/runpod-doc-worker/commit/148156ec1280573219a7bc9a8de8ffb6d0d5d23f))
* **test:** assert the destination mode, not a umask computation ([980a846](https://github.com/sergeyshmakov/runpod-doc-worker/commit/980a846c1042c8a15be475aa35da0b0482ae79ee))

### Refactoring

* bring every file under a 500-line cap, and enforce it ([09c9662](https://github.com/sergeyshmakov/runpod-doc-worker/commit/09c9662aff08123ee9cfe63f31fbc09d67c784d9))
* **client:** split responses.py into modules under 500 lines ([bda2c69](https://github.com/sergeyshmakov/runpod-doc-worker/commit/bda2c69dcebf9a8870f7579ab81a82265c95d0ef))

### Documentation

* **agents:** describe the consumer generically ([6a5590f](https://github.com/sergeyshmakov/runpod-doc-worker/commit/6a5590f0d3b42f5f31fb711e356ba0f78e616235))
* **agents:** never mark a change breaking without asking ([ff59acc](https://github.com/sergeyshmakov/runpod-doc-worker/commit/ff59acc383f914eac28536ade04bad3571f49b00))
* drop review-process narration from comments and docstrings ([8503ea8](https://github.com/sergeyshmakov/runpod-doc-worker/commit/8503ea824a99e20e90094ba80a0686a6c9ef9cd2))
* **net:** say which URL check is the complete one ([cb9536d](https://github.com/sergeyshmakov/runpod-doc-worker/commit/cb9536db4fefd3ae7c43860d3b4e49e8ddc40d69))

## [0.5.0](https://github.com/sergeyshmakov/runpod-doc-worker/compare/v0.4.0...v0.5.0) (2026-08-21)

### ⚠ BREAKING CHANGES

* **obs:** probe_enabled() and WorkerConfig.probe_default are removed, <PREFIX>_ENABLE_PROBE and <PREFIX>_DISABLE_PROBE are no longer read, and probe_filesystem() no longer raises PermissionError. A worker that wants a gate implements one: it reads whatever variable it documents and calls probe_filesystem() only when it decides to. A worker that had probe_default=True deletes that line and keeps calling the function. The hyphenated token is the spelling git can parse as a trailer, since a trailer token may not contain a space.

### Refactoring

* **obs:** hand probe policy back to the worker ([2392cdb](https://github.com/sergeyshmakov/runpod-doc-worker/commit/2392cdbc4152289933d8c382ca076249837637c7))

### Documentation

* keep the migration gate fail-closed ([9e23b6f](https://github.com/sergeyshmakov/runpod-doc-worker/commit/9e23b6fa0e8377044350727afa5648a80945d6fe))
* record who owns a worker-specific knob ([f5765c6](https://github.com/sergeyshmakov/runpod-doc-worker/commit/f5765c69392436542437836a54ed78b7f6e9ec1b))

## [0.4.0](https://github.com/sergeyshmakov/runpod-doc-worker/compare/v0.3.1...v0.4.0) (2026-08-21)

### ⚠ BREAKING CHANGES

* **config:** Before 0.3.1, filesystem probes were enabled by default,
<PREFIX>_DISABLE_PROBE controlled them, and direct probe_filesystem() calls were
not rejected by an internal policy guard. Probes are now disabled by default,
<PREFIX>_ENABLE_PROBE is authoritative, and disabled direct calls raise
PermissionError. Version 0.4.0 adds WorkerConfig.probe_default and accepts
<PREFIX>_DISABLE_PROBE as a deprecated fallback. Unknown non-blank override
values disable probes.

### Features

* **config:** make filesystem probe policy worker-defined ([db5ff55](https://github.com/sergeyshmakov/runpod-doc-worker/commit/db5ff55c99c4fae0123fa2212ac84526ce2d9cfb))

### Documentation

* **repo:** document consumer compatibility policy ([5c20870](https://github.com/sergeyshmakov/runpod-doc-worker/commit/5c208704917bf359da1e53cd862e9c9f61ff3159))

## [0.3.1](https://github.com/sergeyshmakov/runpod-doc-worker/compare/v0.3.0...v0.3.1) (2026-08-21)

### Refactoring

* **runtime:** tighten input and diagnostic boundaries ([e8ad782](https://github.com/sergeyshmakov/runpod-doc-worker/commit/e8ad782bdc978508f2d252803a8272557a924956))

## [0.3.0](https://github.com/sergeyshmakov/runpod-doc-worker/compare/v0.2.0...v0.3.0) (2026-08-21)

### Features

* **contract:** publish the message every loss is logged under ([e72c1cf](https://github.com/sergeyshmakov/runpod-doc-worker/commit/e72c1cf2da6e2bc118565930c34944b6f4876dc1))
* **transport:** let a caller keep the degradation report ([00f9ba6](https://github.com/sergeyshmakov/runpod-doc-worker/commit/00f9ba6d9a6d332ee40be69c9973d7e25104b8de))
* **transport:** publish the inline payload ceiling ([ca03a94](https://github.com/sergeyshmakov/runpod-doc-worker/commit/ca03a941161c8dcfcddc433f06c3e05e92c0b765))

### Bug Fixes

* **contract:** isolate aggregate degradation items ([bc4572c](https://github.com/sergeyshmakov/runpod-doc-worker/commit/bc4572c37a2cc815ab6505a6978ceb1088218a7b))
* **transport:** keep degradation entries isolated ([5537d16](https://github.com/sergeyshmakov/runpod-doc-worker/commit/5537d1646c7b99caacbf84a2de7555505792ea62))

### Documentation

* warn that an editable install misreports the version ([7b898cc](https://github.com/sergeyshmakov/runpod-doc-worker/commit/7b898cc1a3444fbe68950416fedb3b31f7f85b85))

## [0.2.0](https://github.com/sergeyshmakov/runpod-doc-worker/compare/v0.1.0...v0.2.0) (2026-08-21)

### Features

* **contract:** let a manifest require an artifact ([f849b6b](https://github.com/sergeyshmakov/runpod-doc-worker/commit/f849b6b6447d13f0ed3b7bdb7c138522a96a9813))
* **contract:** report what a response entry lost ([ed59d6a](https://github.com/sergeyshmakov/runpod-doc-worker/commit/ed59d6a618173e03a672350ce1dec8b0eefd3d6d))
* **paths:** tell an unresolvable path from an escaping one ([db033fb](https://github.com/sergeyshmakov/runpod-doc-worker/commit/db033fbe952a7387cd55f09d45a824eb9628e8a9))

### Bug Fixes

* **contract:** bind required artifacts to archives ([1cf4bb2](https://github.com/sergeyshmakov/runpod-doc-worker/commit/1cf4bb2afb1bbc91ad522b84e6af14c187ea3045))
* **contract:** classify a glob hit before discarding non-files ([b3e0b29](https://github.com/sergeyshmakov/runpod-doc-worker/commit/b3e0b29dfac883b5c49d4e8cfc87ffcdb72f86dd))
* **contract:** classify paths before skipping directories ([d529db2](https://github.com/sergeyshmakov/runpod-doc-worker/commit/d529db2ec81787c43e99607c8b99c5a319a948e7))
* **contract:** enforce complete archive responses ([f4a96ba](https://github.com/sergeyshmakov/runpod-doc-worker/commit/f4a96bab542050f3cb9220a743c9fd97fe0759be))
* **contract:** retain broken exact artifact matches ([b9af91d](https://github.com/sergeyshmakov/runpod-doc-worker/commit/b9af91d25039262d316d32a6f4c0ea91a923bce1))
* **contract:** spool archive member snapshots ([166acf1](https://github.com/sergeyshmakov/runpod-doc-worker/commit/166acf1566180b452021cfa138331bf06d348f53))

### Documentation

* **contract:** clarify missing artifact defaults ([5648ba0](https://github.com/sergeyshmakov/runpod-doc-worker/commit/5648ba041d65affc332b594e11a179ae14420528))
* describe degraded responses and required artifacts ([a0b5373](https://github.com/sergeyshmakov/runpod-doc-worker/commit/a0b5373d93edbe36fb06913672cc71ef85cf3200))
* **site:** add Fumadocs GitHub Pages site ([33acc47](https://github.com/sergeyshmakov/runpod-doc-worker/commit/33acc47c7796fa2c02dfec197a3256801586019f))

## [0.1.0](https://github.com/sergeyshmakov/runpod-doc-worker/compare/v0.0.1...v0.1.0) (2026-08-21)

### Features

* add a reusable hub.json validator ([9c92a0d](https://github.com/sergeyshmakov/runpod-doc-worker/commit/9c92a0d423d1967be2d54c9801ed262340bb5122))
* add failure-text redaction and structured logging ([774a0d0](https://github.com/sergeyshmakov/runpod-doc-worker/commit/774a0d0240fdb4d0eec111176670ffd5f790c039))
* add GPU and filesystem debug probes ([dd345d7](https://github.com/sergeyshmakov/runpod-doc-worker/commit/dd345d7b7f9d38909f4790a9cb609ad4408a8081))
* add outbound target checks and input transport ([4c2160a](https://github.com/sergeyshmakov/runpod-doc-worker/commit/4c2160a26784d418721754bab5881b2ca8a4e3b5))
* add response packaging with a declarative artifact manifest ([f7c7392](https://github.com/sergeyshmakov/runpod-doc-worker/commit/f7c7392a2c7750a44e8e1852d624c157f2eac419))
* add worker config with an engine-supplied env prefix ([360c4c5](https://github.com/sergeyshmakov/runpod-doc-worker/commit/360c4c592871c13a9ae24c6bf644d2c518a607c6))
* reject a requested format the manifest does not declare ([bcf0cbe](https://github.com/sergeyshmakov/runpod-doc-worker/commit/bcf0cbecb898aa8930c31b17128863a23f733a8b))

### Bug Fixes

* archive what the engine produced, not what it points at ([8caccd6](https://github.com/sergeyshmakov/runpod-doc-worker/commit/8caccd66a3242d397e278c0039cde4a8f024d221))
* bound the httpx range and report the installed version ([ac8394f](https://github.com/sergeyshmakov/runpod-doc-worker/commit/ac8394fb7bcac7b8436a3e5d47f804f3b4c3f7f1))
* bound the hub scan, the last unbounded read in the module ([02f6fcb](https://github.com/sergeyshmakov/runpod-doc-worker/commit/02f6fcbfbf9de3add2d518790cfd6e6241870812))
* bound the last unbounded listing, and keep what it already knew ([c84f50b](https://github.com/sergeyshmakov/runpod-doc-worker/commit/c84f50ba8074b2b553a2d9bc16a803fa767af363))
* bound the phases a loop cannot see ([8dc0880](https://github.com/sergeyshmakov/runpod-doc-worker/commit/8dc0880a42cb1bf2b3427e7145f6f84e392b41c7))
* bound the probe by what it visits, and let a read error be the answer ([2c79941](https://github.com/sergeyshmakov/runpod-doc-worker/commit/2c799413c8622c2563cd2fc1c29883982e65f622))
* bound the probe on entries read, using a primitive that is lazy ([fa79366](https://github.com/sergeyshmakov/runpod-doc-worker/commit/fa7936689250c3de43de28f2b4275ec70ec86426))
* bound what a probe walks and what a download costs ([1ae0196](https://github.com/sergeyshmakov/runpod-doc-worker/commit/1ae01966f2b8e8e89f138ffc37948908bbdecc43))
* contain snapshot directory reads ([9604cbd](https://github.com/sergeyshmakov/runpod-doc-worker/commit/9604cbd9e47356bdc72ef07d4507004325dcd931))
* give the mirror-failure record the fields every other record has ([3855434](https://github.com/sergeyshmakov/runpod-doc-worker/commit/385543449cbb43b9971d4698c6b84b2413070195))
* guard the iteration, not the call that starts it ([3f2e03a](https://github.com/sergeyshmakov/runpod-doc-worker/commit/3f2e03ab331cbbe3ae58f77747c0dd2eddb5bf0d))
* honor the effective Hugging Face cache ([c85ed56](https://github.com/sergeyshmakov/runpod-doc-worker/commit/c85ed561f31dac628a119ecadafca500733e2377))
* keep a text record on one line ([0803bfc](https://github.com/sergeyshmakov/runpod-doc-worker/commit/0803bfc2f92ed496c595b442bb5a937738e99930))
* keep an artifact read inside the directory it was given ([9476302](https://github.com/sergeyshmakov/runpod-doc-worker/commit/947630284e32c9358cfbae633af315031483ba08))
* keep snapshot fallbacks honest and contained ([c998ebf](https://github.com/sergeyshmakov/runpod-doc-worker/commit/c998ebfa87013315ed3c490853743dee68557473))
* keep the probe working on the caches it exists to diagnose ([542a1d1](https://github.com/sergeyshmakov/runpod-doc-worker/commit/542a1d100a2bb997e3ff810d9480fcccad90b70a))
* let a worker declare where its inputs may come from ([1ff414c](https://github.com/sergeyshmakov/runpod-doc-worker/commit/1ff414cb0fdac95154fdff99fd63beb338a070b9))
* let a worker register its own second log sink ([12a85e4](https://github.com/sergeyshmakov/runpod-doc-worker/commit/12a85e44decb6beb618ad6036747e32a5d9b40c6))
* normalize Hub validator roots ([0876304](https://github.com/sergeyshmakov/runpod-doc-worker/commit/0876304bd40ce7e281111042ce53be6164081cde))
* preserve partial cache diagnostics ([f33a0da](https://github.com/sergeyshmakov/runpod-doc-worker/commit/f33a0daa8f741bdd98fb421143df9114516e4ef8))
* read a manifest once, and normalise a path before judging it ([9c92c0a](https://github.com/sergeyshmakov/runpod-doc-worker/commit/9c92c0a58cba709da8d21b79c7f29448f41310ed))
* refuse an entry the caller did not ask for ([bcbe5b5](https://github.com/sergeyshmakov/runpod-doc-worker/commit/bcbe5b5bdb3fc985a77fad1f5261f682920e4f44))
* refuse an inline payload that is empty once whitespace is removed ([ff3f523](https://github.com/sergeyshmakov/runpod-doc-worker/commit/ff3f523f30ed528635e813f88b3d4c96cdb561e6))
* refuse legacy numeric host spellings ([6f655e7](https://github.com/sergeyshmakov/runpod-doc-worker/commit/6f655e7f0ddbf2db05dd7dc85d5288bd4354f535))
* reject malformed inline base64 at the transport boundary ([035dcf5](https://github.com/sergeyshmakov/runpod-doc-worker/commit/035dcf5cd40f9cceaface159b0b89da8b2afa81a))
* reject unresolvable containment paths ([91b1ca7](https://github.com/sergeyshmakov/runpod-doc-worker/commit/91b1ca7f0deb738fa75d60ef1d05a2342e6e2d03))
* report partial model cache selections ([6d3f80c](https://github.com/sergeyshmakov/runpod-doc-worker/commit/6d3f80c28e8929d750dd1e42801aa666ac7bab0a))
* report unresolvable volume paths ([c240942](https://github.com/sergeyshmakov/runpod-doc-worker/commit/c24094263b35b772124c2cd5b4b573dba2d44da0))
* return the same artifacts whichever container was asked for ([cce7fe7](https://github.com/sergeyshmakov/runpod-doc-worker/commit/cce7fe710b41f3e0745f4d5685771ec0a030d617))
* say a hub scan was truncated instead of calling it a miss ([c576289](https://github.com/sergeyshmakov/runpod-doc-worker/commit/c57628966261eca6cd23e1a58f2d81754868483f))
* stop artifacts sharing state, guessing, or reporting partial output ([1387ca4](https://github.com/sergeyshmakov/runpod-doc-worker/commit/1387ca4c2d64b32c23ab06b4eb2633e029e871ac))
* stop engine data overwriting the fields the harness owns ([2450f54](https://github.com/sergeyshmakov/runpod-doc-worker/commit/2450f540bc59359adf62944bc77cc68435508618))
* stop one unusable cache entry deciding the whole answer ([9ec4054](https://github.com/sergeyshmakov/runpod-doc-worker/commit/9ec4054a14a1d0b0ac9c68c5a08c02e1b03c6bf2))
* stop the probe reading a directory it is not going to show ([9cffb66](https://github.com/sergeyshmakov/runpod-doc-worker/commit/9cffb6624e34b99925fdb6aaa618adbb4ba86c62))
* treat multicast answers as not publicly routable ([a4c55d3](https://github.com/sergeyshmakov/runpod-doc-worker/commit/a4c55d3b1176f25377bb02cf8eaebd54b361fc8c))
* validate the name an archive gives a member, not only its source ([e6182b9](https://github.com/sergeyshmakov/runpod-doc-worker/commit/e6182b9aab9c88d5d6f56da1d7386952199a9999))

### Refactoring

* one answer to whether a path stays inside its tree ([cbe3cd8](https://github.com/sergeyshmakov/runpod-doc-worker/commit/cbe3cd800c8ae19eadfa51972cb42bc4d9e80f2d))

### Build / Deps

* commit the npm lockfile for reproducible CI installs ([4bd7d41](https://github.com/sergeyshmakov/runpod-doc-worker/commit/4bd7d41d7e67572629f85645c84719a46b2b20da))
* stop a single commit from taking the package to 1.0.0 ([2076a76](https://github.com/sergeyshmakov/runpod-doc-worker/commit/2076a7643a63bd35bb9df8a46ab61ca94b5d38cb))
