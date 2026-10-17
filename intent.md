# System Intent

The system aims to **preserve the full history of a development exploration and manage the experiments needed to evaluate it**. That history should remain useful across branches, machines, and AI sessions.

Its central idea is to connect:

> **Intent → code changes → experiments → evidence → conclusions → next steps**

This document is a starting brief for implementations of that idea.

## Purpose

Build a system for developers and AI agents working on complex repositories, especially where builds and tests take a long time. It should help answer:

- Why does this branch or workspace exist?
- What approaches have already been tried?
- Which code and configuration produced this result?
- What is currently running, and where are its logs?
- What evidence supports the current conclusion?
- What should the next developer or AI session do?

## Core Concepts

| Concept | Responsibility |
|---|---|
| **Project** | Identifies the repository, environment, and available workflows. |
| **Feature / exploration** | Records a goal or hypothesis, progress, decisions, and conclusions. Can contain smaller explorations. |
| **Workspace** | Provides the code checkout and development environment associated with an exploration. |
| **Runset** | Groups experiments performed to answer a particular question, with recorded code and configuration inputs. |
| **Task / attempt** | Represents scheduled work and each execution attempt, including resources, status, failures, and outputs. |
| **Artifact** | Stores logs, binaries, reports, traces, and other outputs. |
| **Evidence** | Connects a conclusion to the experiments and artifacts supporting it. |

A feature can span many code versions and runsets. Its identity persists after branches are merged or deleted.

## Typical Workflow

1. Create an exploration and describe its objective.
2. Create or associate a workspace.
3. Implement an approach.
4. Submit a workflow, such as build → parallel tests → analysis.
5. Let the scheduler manage execution independently of the terminal or AI session.
6. Collect results and compare them against a baseline.
7. Record a conclusion with links to supporting evidence.
8. Resume later with enough context to continue.

## Execution Responsibilities

A background service owns queues, resource allocation, dependencies, timeouts, cancellation, retries, and log collection. Jobs survive the end of an interactive session.

Each experiment records its actual inputs and outputs. A retry with unchanged inputs is distinguishable from a new experiment using changed code or parameters.

## AI Collaboration Responsibilities

Expose the same underlying operations through a CLI and structured APIs, optionally including MCP.

An agent should be able to retrieve a concise context bundle containing the objective, workspace, decisions, recent results, active tasks, and next steps. It can then request detailed evidence when needed.

Waiting and monitoring belong to ordinary background software. Agent reasoning is triggered when results arrive, failures need diagnosis, or a decision is required.

## Extensibility Boundary

Keep project-specific knowledge behind adapters:

| General system manages | Project adapter supplies |
|---|---|
| Scheduling and dependencies | Build and test commands |
| Resource requests and execution state | Environment setup |
| Input and artifact records | Test discovery and parameter expansion |
| Progress and result interfaces | Progress parsing and success criteria |
| Result storage and comparisons | Metrics, aggregation, and comparison rules |

A hardware project might supply simulation checkpoints and IPC metrics. A compiler project might supply test suites and compile-time measurements. The surrounding exploration workflow stays applicable.

## Principles

- Give explorations stable identities independent of directory and branch names.
- Keep experimental inputs traceable to actual execution.
- Preserve unsuccessful approaches and their reasoning.
- Make conclusions reference evidence.
- Support resuming work without reconstructing the previous conversation.
- Let humans and agents share one source of truth.
- Start with one complete workflow, then add adapters and execution backends as real needs emerge.

## First Implementation Goal

Demonstrate this single scenario: **start an exploration, submit a long-running experiment, close the session, then return in a fresh session and understand both the result and what to do next.**
