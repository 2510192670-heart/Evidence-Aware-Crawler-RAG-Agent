# AGENTS.md

## Project Overview

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

It is NOT a general-purpose crawler and should not be expanded beyond the current milestone without explicit user approval.

---

# Core Architecture Principle

The most important architectural rule is:

**LLM proposes. Deterministic code validates and executes.**

The LLM is a planner, not a trusted executor.

Prefer deterministic program logic whenever a task can be solved reliably without an LLM.

Never execute arbitrary Python, JavaScript, shell commands, or other code generated directly by the LLM.

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
- GET requests
- page-number pagination
- maximum 10 pages
- local execution
- single task
- single worker

Currently unsupported:

- public Internet targets
- POST pagination other than JSON-body page-number pagination (see M4.3 POST JSON Capability Rules)
- cursor pagination
- offset pagination
- authentication workflows
- CAPTCHA
- multi-user systems
- distributed workers

Do not silently expand these capabilities.

---

## M4.3 POST JSON Capability Rules

### Supported Capabilities

- GET query page-number pagination
- POST JSON-body page-number pagination

### POST Safety Boundary

Allowed:

- loopback targets
- `application/json` content type
- top-level JSON object body
- page-number pagination

Forbidden:

- arbitrary POST
- form / multipart bodies
- mutation endpoints
- authentication replay
- cookie / token forwarding

### Evidence-Driven Execution

- the model cannot invent the request method
- GET and POST cannot be converted into each other
- execution may only mutate the page field

### Collector Rules

- generated collectors must be standalone
- standard library only
- no backend imports
- GET collectors must remain backward compatible
- POST collectors use a separate template path

### Benchmark Rule

A case may be marked `supported=true` only when it has all of:

- observation
- planning
- execution
- export
- verification

### Current Milestone

M4.3.3: POST cURL import + deterministic collector export

---

# Network Security Boundary

Target validation is part of the system design and must not be weakened.

Browser-side and HTTP-client-side target restrictions must remain consistent.

Do not bypass target validation for convenience.

Do not introduce shell execution for imported cURL commands.

cURL import must remain a parser for a controlled subset rather than a shell execution mechanism.

---

# Sensitive Data

Sensitive information must not be leaked into:

- prompts
- RAG documents
- logs
- artifacts
- exported collectors

Examples include:

- Authorization
- Cookie
- API keys
- access tokens
- secrets

Reuse the project's existing redaction and contract logic whenever possible.

Do not create an independent conflicting redaction implementation.

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

1. inspect the existing implementation;
2. inspect related tests;
3. identify the smallest integration point;
4. preserve backward compatibility whenever possible.

Avoid introducing parallel implementations of existing functionality.

---

# Development Workflow

For every development task, follow:

Inspect
→ Understand
→ Design
→ Test
→ Implement
→ Targeted Test
→ Full Regression Test
→ Live Verification

Do not begin implementation before inspecting the relevant existing code.

Prefer small, reviewable changes.

Do not perform unrelated refactoring while implementing a feature.

---

# Testing Rules

Existing passing tests are regression protection.

Never:

- delete a test simply because a new implementation fails it;
- weaken assertions merely to make tests pass;
- replace meaningful integration tests with mocks;
- hide failures using broad exception handling.

When adding behavior:

1. add or update appropriate tests;
2. run targeted tests;
3. run the full test suite.

Live pipeline features should also receive real local integration verification when practical.

---

# Real Verification

Mock tests are useful for isolated components but must not replace end-to-end verification of critical pipeline behavior.

For features involving actual collection:

prefer verification against the project's controlled loopback test site.

Where applicable verify:

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

Existing artifact names and schemas should be treated as compatibility contracts.

Do not rename or remove existing artifacts without explicit approval.

New features should preferably add artifacts rather than mutate unrelated existing ones.

---

# LLM Budget

Respect existing model-call and timeout budgets.

Do not increase model calls merely to improve convenience.

Before adding another LLM call, determine whether deterministic code can perform the same operation.

---

# Code Quality

Prefer:

- small functions
- explicit contracts
- clear error types
- deterministic behavior
- reuse of existing validators
- separation between planning and execution

Avoid:

- unnecessary abstraction
- premature framework introduction
- duplicate validators
- hidden side effects
- broad catch-all exception handling
- large unrelated refactors

Comments should explain **why**, especially for non-obvious constraints.

---

# Dependency Policy

Do not add dependencies unless they provide clear value that cannot reasonably be achieved using existing dependencies or the Python standard library.

Do not perform broad dependency upgrades as part of unrelated feature work.

Dependency changes must be explicitly reported.

---

# Git Rules

Before significant work, inspect:

git status
git branch
git log --oneline --decorate -10

Never perform without explicit user approval:

- force push
- destructive reset
- history rewriting
- deleting branches

Do not push automatically.

At the end of a development task report:

- git status
- git diff --stat
- tests executed
- suggested commit message

---

# Scope Discipline

Work on one milestone at a time.

Do not implement future roadmap features merely because they appear useful.

If a task appears to require a major architectural change, stop and explain:

1. why the current architecture cannot support it;
2. which modules would need to change;
3. the regression risks;
4. the smallest viable alternative.

Wait for user approval before proceeding with major architectural changes.

---

# Current Roadmap

The intended high-level roadmap is:

M3
Deterministic collector export and verification

M4
Expanded controlled collection capabilities and bounded repair

M5
Improved RAG architecture

M6
Frozen evaluation datasets, comparative experiments, resource measurements, dependency locking, and portfolio-quality evaluation reports

Complete and stabilize each milestone before moving to the next.

---

# Communication

When a task is completed, provide a concise engineering report containing:

## Changes
Files changed and purpose.

## Tests
Targeted tests and full regression results.

## Live Verification
Real verification performed, when applicable.

## Artifacts
New or modified artifacts.

## Known Limitations
Remaining boundaries or risks.

## Git
Current status, diff summary, and suggested commit message.

Do not claim success solely because code was written.

Success requires verification.