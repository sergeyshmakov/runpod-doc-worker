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
