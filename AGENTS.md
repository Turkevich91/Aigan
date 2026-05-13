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

## Safety Rules

- Never put secrets, `.env` contents, raw prompts, private chat text, API keys, Telegram tokens, or full user messages into GitHub issues, project cards, commits, or logs.
- Use sanitized summaries and short redacted previews only.
- Prefer behavior-level descriptions and reproduction notes over private payloads.

## Implementation Rules

- Keep business logic in tested Python modules.
- Use prompt/policy files for language and behavior contracts, not for hidden state or untestable control flow.
- Use OpenAI Agents SDK hooks/guardrails for observability and safety where useful.
- Do not add LangGraph for v1 self-analysis; revisit only for long-running approval/checkpoint workflows.
- Ordinary group messages must remain silent unless there is explicit invocation, reply-to-bot, private DM, or pending context.
