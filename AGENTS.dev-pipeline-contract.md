# Agent Dev Pipeline Contract

This contract describes the public, environment-obfuscated workflow for Codex-like agents working on Aigan. Its goal is to let an agent carry a task from epic selection to merge and project bookkeeping without requiring a human to sit beside every wait state.

Private runtime details, host aliases, local paths, MCP endpoints, logs, databases, media caches, secrets, and raw chat text must stay out of this file, GitHub issues, PRs, commits, and public handoffs.

## Operating Rules

- Start from a GitHub issue. Use `[DEV]` for developer workflow, automation, and agent-process contract work.
- Keep `AGENTS.md` as the concise entrypoint and use this file for the detailed pipeline.
- Before implementation, move the task Project status to `In Progress`; new planning work starts as `Todo`.
- Work on a task branch, commit intentional changes, push, and open a PR.
- Request Copilot review on the PR. Treat Copilot as a dry code reviewer: useful for code-level risk, not a context owner and not an approval authority.
- Before ending a ReAct session while waiting on Copilot, CI, deploy validation, another agent, or an external reviewer, create a Heartbeat follow-up for the current thread.
- If Copilot comments, evaluate each point with full project context. Fix relevant issues; briefly document intentionally rejected or non-actionable suggestions when useful.
- Re-request Copilot review after pushing review fixes and create another Heartbeat before sleeping again.
- When Copilot has no relevant new comments, spawn or ask a reviewer sub-agent for a final blocker-focused pass, then run the final tests.
- Mark the PR ready only after the final gates pass. Squash merge, delete the branch, close/update the task issue, and update the parent epic checklist when applicable.

## Epic Task State Machine

```mermaid
stateDiagram-v2
    [*] --> GatherContext
    GatherContext --> SelectEpic: find active epic or standalone task
    SelectEpic --> SelectTask: choose next Todo task
    SelectTask --> AcceptTask: confirm scope and acceptance
    AcceptTask --> PrepareBranch: set Project status In Progress
    PrepareBranch --> Implement: clean git, create branch
    Implement --> VerifyLocal: code or docs change complete
    VerifyLocal --> CommitPush: tests and safety checks pass
    CommitPush --> OpenPR: commit and push
    OpenPR --> RequestCopilot: create or update PR
    RequestCopilot --> SleepWithHeartbeat: request Copilot review
    SleepWithHeartbeat --> CheckCopilot: Heartbeat wakes thread
    CheckCopilot --> EvaluateCopilot: comments found
    CheckCopilot --> FinalReviewer: no relevant new comments
    EvaluateCopilot --> Implement: relevant finding
    EvaluateCopilot --> RequestCopilot: pushed fix or documented rejection
    FinalReviewer --> VerifyFinal: reviewer sub-agent finds no blockers
    FinalReviewer --> Implement: reviewer finds blocker
    VerifyFinal --> MergePR: final tests pass and PR mergeable
    MergePR --> CloseTask: squash merge and delete branch
    CloseTask --> UpdateEpic: task summary and Project status Done
    UpdateEpic --> SelectTask: epic still has Todo tasks
    UpdateEpic --> [*]: epic complete
```

## Review And Heartbeat Sequence

```mermaid
sequenceDiagram
    participant CA as Codex Agent
    participant GH as GitHub Project and PR
    participant CP as Copilot Reviewer
    participant HB as Heartbeat
    participant RA as Reviewer Sub-Agent

    CA->>GH: create or select task issue
    CA->>GH: set task In Progress
    CA->>GH: push branch and open PR
    CA->>CP: request PR review
    CA->>HB: schedule wake-up before ending ReAct session
    HB-->>CA: wake and check PR review
    CA->>GH: read Copilot comments and PR state
    alt Copilot has relevant findings
        CA->>CA: decide with full project context
        CA->>GH: push fix or document rejection
        CA->>CP: request re-review
        CA->>HB: schedule next wake-up
    else Copilot has no relevant comments
        CA->>RA: request blocker-focused final review
        RA-->>CA: return findings or mergeable verdict
        CA->>GH: run final gates, mark ready, squash merge
        CA->>GH: delete branch, close task, update epic
    end
```

## Merge Gate Flow

```mermaid
flowchart TD
    A["Task implementation complete"] --> B["Local focused checks"]
    B --> C{"Checks pass?"}
    C -- "No" --> D["Fix failures before PR"]
    D --> B
    C -- "Yes" --> E["Commit, push, open PR"]
    E --> F["Request Copilot review"]
    F --> G["Create Heartbeat before sleep"]
    G --> H{"Copilot has comments?"}
    H -- "Relevant" --> I["Fix or consciously reject with sanitized note"]
    I --> J["Run focused tests and push"]
    J --> F
    H -- "None or not relevant" --> K["Run reviewer sub-agent"]
    K --> L{"Reviewer found blocker?"}
    L -- "Yes" --> I
    L -- "No" --> M["Run final test suite and safety checks"]
    M --> N{"PR mergeable and not draft?"}
    N -- "No" --> O["Resolve merge/draft/status issue"]
    O --> M
    N -- "Yes" --> P["Squash merge and delete branch"]
    P --> Q["Update task issue and epic checklist"]
    Q --> R["Select next epic task or finish"]
```

## Handoff Checklist

- Current task issue and Project status are named.
- Branch, PR number, latest commit, and test status are named.
- Heartbeat is scheduled for any wait state before the agent stops.
- Copilot comments are classified as relevant, not relevant, duplicate, or already fixed.
- Final reviewer sub-agent verdict is recorded before merge.
- Task issue gets a concise completion note with important pitfalls, decisions, and verification.
- Parent epic checklist is updated after task completion when a parent epic exists.

## References

- AGENTS.md format: https://github.com/agentsmd/agents.md
- Custom instruction guidance: https://code.visualstudio.com/docs/copilot/customization/custom-instructions
- Copilot code review: https://docs.github.com/en/copilot/how-tos/use-copilot-agents/request-a-code-review/use-code-review?tool=visualstudio
- Mermaid state diagrams: https://mermaid.js.org/syntax/stateDiagram
