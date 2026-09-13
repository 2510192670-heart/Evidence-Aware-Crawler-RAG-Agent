# AGENTS.md

# Project Overview

This repository contains **Web Data Agent**, a local learning and demonstration project for AI-assisted web data collection.

The current system pipeline is:

User Task

→ Browser / cURL Observation

→ Evidence

→ Redaction & Schema

→ BM25 Case Retrieval

→ LLM Planning

→ Plan Validation

→ Execution

→ Verification

→ Artifacts

→ SQLite

The system is intentionally limited in scope.

It is NOT a general-purpose crawler.

Do not expand system capabilities beyond the current milestone without explicit user approval.

---

# Core Architecture Principle

The most important architectural rule is:

**LLM proposes. Deterministic code validates and executes.**

The LLM is a planner, not a trusted executor.

Prefer deterministic program logic whenever a task can be solved reliably without an LLM.

Never execute arbitrary:

- Python
- JavaScript
- shell commands
- generated scripts

directly from LLM output.

All model-generated plans must pass deterministic validation before execution.

---

# Current Technology Stack

Backend:

- Python
- FastAPI
- Playwright
- httpx
- SQLite
- Alembic
- rank_bm25

Frontend:

- Vue 3
- TypeScript
- Vite

LLM:

- DeepSeek

Current RAG:

- BM25Okapi
- manually curated cases
- RAG can be enabled or disabled

Do not replace the current RAG implementation unless the active milestone explicitly requires it.

---

# Current Capability Boundary

The current supported collection scope is intentionally restricted to:

- literal loopback targets
- same-origin GET requests
- same-origin JSON POST requests
- page-number pagination
- maximum 10 pages
- local execution
- single task
- single worker

Currently unsupported:

- public Internet targets
- cursor pagination
- offset pagination
- authentication workflows
- CAPTCHA
- multi-user systems
- distributed workers
- arbitrary POST requests

Do not silently expand these capabilities.

---

# M4.3 GET / POST JSON Capability Rules

## Supported Capabilities

Supported:

- GET query page-number pagination
- POST JSON-body page-number pagination

The POST capability is intentionally limited.

---

## POST Safety Boundary

Allowed:

- loopback targets
- application/json content type
- top-level JSON object body
- page-number pagination

Forbidden:

- arbitrary POST
- form bodies
- multipart bodies
- mutation endpoints
- authentication replay
- cookie/token forwarding

---

## Evidence-Driven Execution

The request method is determined by evidence.

Rules:

- the model cannot invent the request method
- GET and POST cannot be converted into each other
- execution may only mutate the observed page field
- pagination location must match observed evidence

Allowed pagination mutation:

- query page field
- JSON body top-level page field

Not allowed:

- modifying unrelated request fields
- inventing hidden parameters
- changing request semantics

---

# Imported Request Rules

Imported requests are evidence, not executable commands.

For cURL imports:

- parsing happens only inside curl_import
- downstream pipeline receives structured metadata
- do not reparse cURL text after import
- preserve GET backward compatibility
- POST imports must preserve method and JSON body shape

Imported request metadata:

- method
- request body

must be treated as validated evidence.

Sensitive request metadata must be rejected before task creation.

Do not allow imported requests to bypass normal validation.

---

# Collector Rules

Generated collectors are deterministic execution artifacts.

Rules:

- collectors must be standalone
- collectors must use standard library only unless explicitly approved
- collectors must not import backend modules
- collectors must not execute arbitrary generated code

GET and POST collectors use separate template paths.

Additional requirements:

- GET collector behavior is frozen
- POST collector must not modify GET behavior
- exported collector results must be verified against internal executor results
- changes to collector templates require explicit review

The collector is not trusted because it was generated.

It is trusted only after deterministic verification.

---

# Benchmark Rules

Benchmark datasets are evaluation contracts.

A case may be marked:
supported=true

only when it has:

- observation
- planning
- execution
- export
- verification

Do not modify:

- benchmarks/tasks.json
- benchmarks/tasks.sha256

without explicit milestone approval.

Benchmark changes require:

- updated hash
- regression verification
- documented reason

---

# Network Security Boundary

Target validation is part of the system design.

Browser-side and HTTP-client-side restrictions must remain consistent.

Do not bypass target validation for convenience.

Do not introduce shell execution for imported cURL commands.

cURL import is a parser for a controlled subset, not a command execution mechanism.

---

# Sensitive Data

Sensitive information must not leak into:

- prompts
- RAG documents
- logs
- artifacts
- exported collectors

Examples:

- Authorization
- Cookie
- API keys
- access tokens
- secrets

Reuse existing redaction and contract logic.

Do not create conflicting independent redaction systems.

---

# Pipeline Stability

The existing pipeline is considered a stable contract:

Observe

→ Evidence

→ Redaction

→ Retrieval

→ Planning

→ Validation

→ Execution

→ Verification

→ Artifact persistence

Before changing pipeline behavior:

1. inspect existing implementation
2. inspect related tests
3. identify smallest integration point
4. preserve backward compatibility

Avoid parallel implementations.

---

# Development Workflow

For every development task:

Inspect

→ Understand

→ Design

→ Test

→ Implement

→ Targeted Test

→ Full Regression Test

→ Live Verification

Do not begin implementation before inspecting relevant code.

Prefer small, reviewable changes.

Do not perform unrelated refactoring.

---

# Testing Rules

Existing passing tests are regression protection.

Never:

- delete tests because implementation fails
- weaken assertions
- replace meaningful integration tests with mocks
- hide failures using broad exception handling

When adding behavior:

1. add/update tests
2. run targeted tests
3. run full regression tests
4. perform live verification when applicable

Milestone changes should report:

- tests executed
- benchmark impact
- artifact impact

---

# Real Verification

Mock tests are useful for isolated components.

They must not replace end-to-end verification.

For collection features:

prefer controlled loopback verification.

Verify:

- task completion
- page count
- record count
- schema
- uniqueness
- completeness
- artifact generation

---

# Artifact Compatibility

Task artifacts are stored under:
data/tasks/{task_id}/

Existing artifacts are compatibility contracts.

Do not:

- rename artifacts
- remove artifacts
- change schemas

without explicit approval.

Prefer adding new artifacts.

---

# LLM Budget

Respect existing:

- model-call limits
- timeout budgets

Do not add LLM calls for convenience.

Before adding an LLM step, determine whether deterministic code can solve the problem.

---

# Bounded Repair Rules

Repair is controlled behavior.

It is not unlimited retry.

Any repair mechanism must:

- be driven by registered error taxonomy
- only handle explicitly repairable errors
- have bounded retry count
- validate repaired plans before execution
- verify repaired results afterward

Never:

- allow unrestricted LLM self-modification
- retry forever
- bypass validation
- repair security failures automatically
- directly modify executable collectors

---

# Code Quality

Prefer:

- small functions
- explicit contracts
- clear error types
- deterministic behavior
- existing validators
- separation between planning and execution

Avoid:

- unnecessary abstraction
- premature frameworks
- duplicate validators
- hidden side effects
- broad exception catching
- unrelated refactoring

Comments should explain why.

---

# Dependency Policy

Do not add dependencies unless necessary.

Do not perform broad dependency upgrades during unrelated work.

Dependency changes require explicit reporting.

---

# Git Rules

Before significant work inspect:
git status
git branch
git log --oneline --decorate -10

Never perform without approval:

- force push
- destructive reset
- history rewrite
- branch deletion

Do not push automatically.

At completion report:

- git status
- git diff --stat
- tests executed
- suggested commit message

---

# Scope Discipline

Work on one milestone at a time.

Do not implement future roadmap features because they appear useful.

For major architectural changes explain:

1. why current architecture cannot support it
2. required modules
3. regression risks
4. smallest alternative

Wait for approval before major changes.

---

# Current Roadmap

Completed:

## M3

Deterministic collector export and verification

## M4.1

Benchmark foundation and frozen evaluation datasets

## M4.2

Error taxonomy and structured failure reporting

## M4.3

GET/POST JSON controlled collection capability

Implemented:

- POST observation
- POST execution
- POST cURL import
- imported metadata propagation
- deterministic POST collector export


Current milestone:

## M4.4

Bounded repair

Goal:

Controlled error-driven plan repair with deterministic validation.

---

# Communication

When a task is completed, provide:

## Changes

Files changed and purpose.

## Tests

Targeted tests and full regression results.

## Live Verification

Real verification performed.

## Artifacts

New or modified artifacts.

## Known Limitations

Remaining boundaries and risks.

## Git

Current status, diff summary, suggested commit message.

Do not claim success only because code was written.

Success requires verification.