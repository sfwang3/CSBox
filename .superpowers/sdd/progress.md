# CSBox usable core SDD progress

Plan: docs/superpowers/plans/2026-08-10-csbox-usable-core.md
Baseline: ebac668

Task 1: complete (commits ebac668..002171e, review clean)
Task 2: complete (commits 002171e..849d276, review clean)
Task 3: complete (commits 849d276..1174495, review approved)
Minor backlog: tests/test_recorder.py concurrent close test should use a second-thread-start barrier instead of relying only on a 30ms sleep.

# CSBox v0.2 productization SDD progress

Plan: docs/superpowers/plans/2026-08-11-csbox-v0.2-implementation-plan.md
Baseline: 807007a

Task 1: complete (commits a71b816..b3d2feb, review clean after lab JSON schema fix; minor test literal retained)
Task 2: complete (commits 519fa12..b09edc1, review approved after POSIX directory-FD hardening; Windows fallback limitation documented)
Task 3: complete (commits fe5970a..91e8e99, security fixes through 91e8e99; final independent review approved; 42 focused tests)
Task 4 (API variables/scenario): complete (commits 33bce36..325c47e, boundary fixes through 325c47e; final independent review approved; 60 focused tests)
Task 5 (HTTPX transport): complete (commits f0b8a34..355f763, response/redaction fixes through 355f763; final independent review approved; 122 focused tests)
Task 6 (API assertions/runner): complete (commits db52967..d82744b, raw-view/copy and nested redaction fixes through d82744b; final independent review approved; 541 full tests, 1 skipped)
Task 7 (API CLI run): complete (commits b59ef86..a29d6df, parser/config isolation fixes through a29d6df; final independent review approved; 555 full tests, 1 skipped)
Task 8 (API run persistence/list): complete (commits f97ffc5..74dd67c, auto-save/redaction/integrity/TOCTOU hardening through 74dd67c; final independent review approved; 574 full tests, 1 skipped)
Task 9 (OpenAPI import): complete (commits 1a356aa..3c104b9, sensitive-input/structure/staging safety fixes through 3c104b9; final read-only review clean; 583 full tests, 1 skipped)

Task 4: complete (commits 3bfaf3f..f881c56 plus final safety fixes through 11a4879; final independent review clean, 43 focused tests)
Task 5: complete (commits 8cd9e44..de32261, regression review clean)
Task 6: complete (commits c5b35f1..ddf11b4, regression review clean)
Task 7: complete (commit 93000c2, full verification clean)
Task 8: complete (commit 40baee6, full verification clean)
Task 9: complete (commits 6a0b357..af6ee05 plus Windows fixes through 293186b, independent review clean; native Windows validation green in CI run 31460783587)
