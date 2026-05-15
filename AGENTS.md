# Aigan Agent Workflow

This repository is maintained by humans and coding agents such as Codex, Google Antigravity, Claude Code, and other agentic coding tools.

## GitHub Project Planning

- Significant plans must be documented in the GitHub Project before implementation.
- Project: `Turkevich91` project `4` (`Aigan 👾`).
- Repository: `Turkevich91/Aigan`.
- Use GitHub issues as the project item source when possible.
- Set new planning work to `Status=Todo`.

## Issue Origin Prefixes

- `[codex]` means a planning or development issue created by a coding agent or developer workflow.
- `[Aigan]` means an issue created by Aigan itself through self-analysis or complaint-temperature reporting.
- Aigan self-reports are triage signals, not confirmed bugs.
- `[DEV]` means a developer-workflow, automation, or agent-process contract issue.

## Agent Development Pipeline

- Follow the public agent handoff and PR-review contract in [`AGENTS.dev-pipeline-contract.md`](AGENTS.dev-pipeline-contract.md).
- Keep this repository-level entry concise; detailed workflow diagrams and gates live in that contract.
- Keep private environment details in `AGENTS.local.md` only.

## Safety Rules

- Never put secrets, `.env` contents, raw prompts, private chat text, API keys, Telegram tokens, or full user messages into GitHub issues, project cards, commits, or logs.
- Use sanitized summaries and short redacted previews only.
- Prefer behavior-level descriptions and reproduction notes over private payloads.

## Runtime Environment

- Local development can happen from this clone, but live deployment details and durable runtime data are operator-specific and must stay outside the repository.
- Treat the configured deployment environment as the source of truth for SQLite memory, cached media, Telegram imports, logs, Docker volumes, and other generated artifacts.
- Do not commit hostnames, SSH aliases, private absolute paths, runtime artifacts, imports, cached media, local databases, `.env`, or deployment-only secrets into the repository.
- Before debugging behavior that depends on memory, imports, embeddings, media cache, or Docker state, inspect the configured deployment environment from private operator context rather than assuming the local clone has the same data.

## Optional Local Operator Notes

- Maintainers may keep private machine-specific notes in `AGENTS.local.md`.
- `AGENTS.local.md` is optional, local-only, and must never be committed.
- It may contain deployment aliases, private paths, runtime data locations, or operator workflow notes.
- If the file exists, coding agents may read it for local context, but must never copy its contents into commits, GitHub issues, logs, or public documentation.

## Implementation Rules

- Keep business logic in tested Python modules.
- Use prompt/policy files for language and behavior contracts, not for hidden state or untestable control flow.
- Use OpenAI Agents SDK hooks/guardrails for observability and safety where useful.
- Do not add LangGraph for v1 self-analysis; revisit only for long-running approval/checkpoint workflows.
- Ordinary group messages must remain silent unless there is explicit invocation, reply-to-bot, private DM, or pending context.
