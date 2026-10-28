# Advanced Scenario Implementation Plan

**Date:** 2026-10-28
**Scope:** Repository investigation with safe reproduction; evidence-backed research briefs
**Status:** Implemented in the current working tree; bounded by the remaining production limitations recorded below

## Why these two scenarios

These scenarios use the harness's strongest real building blocks: the durable graph runner, tool registry, filesystem/process policy, source-fetch adapter, evidence model, and verification gate. They make the product immediately more useful while exposing and correcting concrete runtime contracts. Both remain advisory workflows; neither will perform an autonomous high-impact external action.

## Shared implementation principles

1. Validate scenario inputs before invoking a tool, model, or external endpoint.
2. Keep model-produced text distinct from tool-observed evidence and deterministic policy decisions.
3. Never label a claim verified merely because a model generated it or a URL exists.
4. Bound source count, input size, output size, command duration, and result counts.
5. Keep protected-file access denied. A run-level resume label must not grant a path or command capability.
6. Persist only information needed by the workflow; traces must not contain full source bodies or secret values. Current run persistence stores input/state and has no general retention/encryption policy, so sensitive research input remains a restricted local-profile use until that foundation is added.
7. Preserve current graph/API result shapes where safe; make changed trust semantics explicit in documentation and eval expectations.

## Scenario A — Repository investigation and safe reproduction

### Current defects to close

- Empty or whitespace search queries are accepted and can match every line.
- Caller-controlled `max_search_results`, command, timeout, and path inputs do not have one graph-level contract.
- The protected-file gate depends on a manually configured list, while the sandbox has a broader default protected-path policy.
- A protected-path pause resumes at `reproduce`; the current generic resume endpoint accepts self-asserted approval labels. That can authorize a command after the protected-path gate even though no independent approval exists.
- A passing command exit is evidence that a command ran, not proof that the hypothesis is true.

### Design and implementation order

1. Add strict graph input parsing for non-empty query, bounded repo path, result limit, reproduction command length, and timeout range. Reject malformed types rather than coercing them into surprising shell strings.
2. Use the active `ToolRegistry.sandbox` protection policy (including mandatory defaults and custom wildcard rules) for investigation candidates.
3. If only protected candidates match, return `needs_human` and set its safe resume node to scoring/finalization. Never resume into reproduction, read, or write. The current approval API may acknowledge completion of the review pause but cannot unlock protected data or execute a command.
4. If no reproduction command was supplied, keep the run read-only and mark runtime verification absent.
5. If a command is supplied, enforce the graph's upper time bound and invoke only `process.run`; retain its exit code and bounded output as execution evidence, and state explicitly that exit success does not prove the reported root cause.
6. Add regression cases for blank/oversized queries, invalid limits/timeouts, default and custom protected wildcard patterns, protected-only search with a resume attempt, absent command, and command output/result semantics.

### Acceptance criteria

- Invalid input causes no search, model call, or process invocation.
- Default protected paths are protected even if `tools.protected_paths` is absent or incomplete.
- Resuming a protected-path pause never invokes `process.run` and never reads protected bytes.
- Command runtime cannot exceed the supported graph maximum; output remains bounded by the sandbox.
- Final evidence distinguishes code observation, command execution, inferred hypothesis, and unverified conclusions.

## Scenario B — Evidence-backed research brief

### Current defects to close

- Inline source records and URL lists are unbounded and weakly typed.
- URL presence is treated as a citation even though the model claim may not be supported by that source.
- The graph invents one generic key point per source instead of validating the model's claim/source relationship.
- Supplied or fetched content is untrusted input and may include instructions; the synthesis prompt does not define a clear untrusted-data boundary.
- The gate currently allows a report to PASS when only source records exist, without claim-level evidence.

### Design and implementation order

1. Validate question, inline source list, source URL list, URLs, titles, content types, per-source bytes, total bytes, and record counts before fetching anything.
2. Assign stable-in-run IDs (`S001`, `S002`, ...), capture retrieval/input kind and SHA-256 content hash, and store no full content in trace payloads.
3. Fetch only HTTP(S) URLs through `web.fetch_url`, preserving the existing outbound network policy. Limit fetch count and truncate/reject content using explicit byte caps.
4. Prompt the model for a small JSON contract: summary plus claims; each claim has one or more source IDs and exact supporting quote excerpts. State in the system message that source content is untrusted data and instructions within it must not be followed.
5. Strictly parse the JSON response. Validate claim count/length, source IDs, quote count/length, and that each quote exists in the referenced source after whitespace normalization. Drop invalid citations and record clear issues; do not fabricate replacement citations.
6. Add an `Evidence` confidence value `source_linked` for a claim whose source ID and excerpt were mechanically validated. This means “a quote exists in this source,” not “the quote logically entails the claim.” Keep semantic entailment as human review until a calibrated independent verifier exists.
7. Make the verification gate return `CONCERNS` for source-linked claims and `NEEDS_HUMAN` for unsupported or malformed claims. It may return `PASS` only for evidence genuinely verified under the graph's defined contract; mere source capture does not qualify.
8. If no source exists, skip synthesis and return an explicit no-evidence result. If model output is malformed, preserve a bounded draft as unverified and do not produce a citation-backed verdict.
9. Add regression/eval cases for malformed input, private/invalid URL rejection by the network tool, prompt-injection text in source content, valid/unknown source IDs, exact/missing quote, malformed model JSON, missing sources, source limits, and honest confidence semantics.

### Acceptance criteria

- Inputs are bounded and validated before network or model calls.
- Every returned citation points to a source record present in this run; the exact quoted excerpt is confirmed to exist in that record.
- Invalid model citations cannot increase `citation_count` or produce a passing verdict.
- Factual entailment is never claimed as verified solely from quote matching; user-visible status says semantic review is still required.
- Prompt-injection text in source content is treated as quoted data and is never promoted to system instructions.
- Traces contain IDs, hashes, counts, and decisions, not entire retrieved source bodies.

## Files expected to change

- `harness/graphs/code_investigate.py` — validation, active sandbox-policy check, safe pause continuation, evidence wording.
- `harness/graphs/research_report.py` — bounded source acquisition, provenance metadata, structured claim/citation validation, honest gate output.
- `harness/engine/schemas.py` and `harness/gates/verification_gate.py` — explicit source-linked evidence semantics and scoring.
- `tests/test_engine_graphs.py` plus focused regression tests — adversarial inputs and workflow boundaries.
- `evals/cases.json` — expected research confidence semantics.
- `docs/ADVANCED_USE_CASE_SCENARIOS.md` and `README.md` — scenario status, examples, and product boundary.

## Verification plan

Run focused graph/regression tests first, then the complete offline suite, Ruff over `harness tests`, `compileall`, and package build. No live provider credentials should be required. A passing local suite does not establish the encryption, tenant isolation, worker recovery, or infrastructure egress controls still identified in the production-readiness review.

## Implementation outcome

- Repository investigation validates query/path/command/limit/timeout inputs before invoking search. It uses the active case-insensitive sandbox policy, does not store protected path names in graph state, caps search results and command duration, and never resumes the protected-path pause at a tool or process node.
- Research validates and bounds inputs before fetching; URL fetches go through the existing public-address-validated, DNS-pinned outbound client. Claims are accepted only when their source IDs are from this run and their quoted excerpts appear in the source. Evidence is labeled `source_linked`; this yields `CONCERNS`, not `PASS`, because quote matching does not prove entailment. Missing, malformed, or unsupported claims require human review.
- The deterministic offline model emits the same structured contract so local behavior is exercised without credentials. Its excerpt-as-claim result is intentionally advisory and requires semantic review.
- The full offline suite passes (180 tests and 12 subtests); Ruff passes for `harness` and `tests`, `compileall`, and source/wheel builds pass. Repository-wide Ruff still reports eight findings in the untouched demo and scratch files. Source bodies still persist in run input/state through the existing SQLite store. Do not use this workflow for confidential or regulated content until encryption, access isolation, retention, and deletion controls are in place.
- These two scenario implementations improve the restricted local workflow. They do not certify the entire harness as public multi-tenant production software; the remaining issues in `PRODUCTION_READINESS_REVIEW.md` still apply.
