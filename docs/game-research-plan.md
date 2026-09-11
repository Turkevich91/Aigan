# Group RPG sessions: research and implementation plan

Updated: 2026-09-11. Status: research/design backlog, not implementation approval.

Design supplement: [D&D-oriented session model and Mermaid diagrams](game-session-model.md). This companion document adds the selected D&D reference, proposed session/participant/authority flows, and verified Telegram privacy constraints; it does not establish an implemented ruleset or close the research gates. The issue body and these documents may represent different revisions; use the documents on the task branch for the latest design discussion.

## Goal and expected value

Create a group text adventure in which a program owns the rules and persistent world state, while an AI game master turns permitted observations and confirmed outcomes into immersive narration. Players make meaningful choices without seeing exact health, damage, defense, or other hidden combat calculations.

The intended value is a coherent shared adventure with low player bookkeeping, persistent NPC relationships, and atmospheric scenes. The main risks are invented outcomes, leaked secrets, growing context/cost, and a technically correct game that is tedious to play.

This is one shaping/research issue. The slices below are a proposed sequence, not an already-approved implementation epic. Promote or split only after the first playable scope and contracts are agreed.

## Established product direction

- Group play is the target. A successful solo demonstration does not prove multiplayer readiness.
- D&D is the selected interaction reference, with the bot as dungeon master. Edition, supported mechanics, and adventure remain to be selected; concealed player HP is an explicit house-rule direction, not a claim of unmodified D&D compatibility.
- The engine owns canonical characters, inventory, positions, turn order, calculations, state changes, and committed events. Narrative text and model memory are not the database.
- Deterministic rule execution does not mean a predetermined story. Player choices may branch the adventure; replay requires the same ruleset, starting state, accepted actions, and recorded random outcomes.
- The game master is not merely an actor playing one character. It must coordinate the scene and NPCs while preserving their identities, knowledge, past interactions, and relationships.
- Exact numerical combat state stays inside the game. Players receive useful qualitative consequences, not a numeric HUD or an easily decoded substitute for exact HP.
- Atmospheric sensory prose is the narrative baseline. Noir is an optional experiment, not a universal mood or automatic style-switching rule.
- A possible physician role may provide approximate assessments; numerical assessment is not yet an accepted mechanic and must never become direct access to exact hidden HP.
- Game context is isolated from ordinary chat history, personal profiles, reminders, and unrelated tools. Ordinary group messages remain silent unless explicitly in the game's interaction scope.
- Character pages are useful, but player-facing dossiers and authoritative internal sheets are different views. A dossier must not accidentally reintroduce the hidden numbers or NPC secrets.

## Research backlog and required outputs

All research tasks remain open. Documentation checks below are preliminary evidence, not successful product trials.

### R1 — Reuse existing systems versus building a small engine

- [ ] Compare existing solutions against actual human multiplayer, programmed rule enforcement, GM-only state, player permissions, NPC records, relationships, narration controls, context management, persistence/export, licensing, and integration effort.
- [ ] Verify bring-your-own-key support separately for each candidate; do not infer it from the presence of an AI model or API-related marketing.
- [ ] Separate a model frontend, an engine, and a hosted adventure product. A room containing several AI characters is not proof of group play by humans.
- [ ] Produce an evidence matrix with supported, unsupported, and unverified cells, plus a build/adapt/reuse recommendation. No account creation, installs, paid evaluation, or credential submission is authorized by this plan.

Initial candidates from the newly examined material:

| Candidate | Documentation currently supports | Important limit / next check |
| --- | --- | --- |
| AI Dungeon | Human multiplayer; JavaScript input/context/output hooks and persistent script state; story cards and context controls | Hooks are an extension point, not proof of an authoritative engine. Inspect host/player permissions, hidden-state access, BYOK, concurrency, and export. |
| Marinara Engine | Single-player Game Mode; world/party/NPC/item/quest state; engine-calculated combat rounds; lorebooks and session summaries; API connections | Alpha status; numerical HP/MP are exposed; special free-form actions still involve the GM. Human multiplayer, strictness outside combat, and protected GM-only views remain unverified. |
| SillyTavern | API connections, character cards, lorebooks, context tools, and configurable narration | Official Group Chats means multiple bots. No verified authoritative RPG engine or human multiplayer in that feature. |
| TextGen / oobabooga | Local model serving, character/chat templates, custom tools, and an API | Primarily a model runtime, not a confirmed game-state or multiplayer solution. |
| AI Game Master | Characters, inventory, companions, progression, combat, and configurable adventure context | Official guide says custom rules cannot be enforced; online mode is marked forthcoming in the checked guide. Do not count Group Adventure as proven online human multiplayer. |

Primary references, checked 2026-09-11:

- [AI Dungeon multiplayer](https://help.aidungeon.com/faq/do-you-support-multiplayer), [scripting](https://help.aidungeon.com/scripting), [story cards](https://help.aidungeon.com/faq/story-cards).
- [Marinara repository](https://github.com/Pasta-Devs/Marinara-Engine), [Game Mode](https://github.com/Pasta-Devs/Marinara-Engine/blob/main/docs/game/getting-started.md), [combat](https://github.com/Pasta-Devs/Marinara-Engine/blob/main/docs/game/combat.md), [party and NPCs](https://github.com/Pasta-Devs/Marinara-Engine/blob/main/docs/game/party-and-npcs.md).
- [SillyTavern API connections](https://docs.sillytavern.app/usage/api-connections/), [Group Chats definition](https://docs.sillytavern.app/usage/core-concepts/groupchats/).
- [TextGen repository](https://github.com/oobabooga/textgen).
- [AI Game Master full guide](https://www.aigamemaster.app/full-guide), [start guide](https://www.aigamemaster.app/how-to-play).

Retain earlier reference cases as comparison material, not exact substitutes: [Sindome qualitative health](https://www.sindome.org/help/game/health/) and [hidden stats](https://www.sindome.org/help/game/substats/); [Kriegsspiel's GM/player information separation](https://kriegsspiel.org/how-to-play/); [Avrae combat state](https://avrae.readthedocs.io/en/stable/cheatsheets/dm_combat.html) and [FIREBALL research](https://arxiv.org/abs/2305.01528); [Friends & Fables](https://fables.gg/) and its [documented AI-interpreted features](https://fables.gg/patch-notes/patch-notes-2547-class-features-subclasses-feats); [Werewolf for Telegram](https://www.tgwerewolf.com/).

### R2 — Bound the first genuinely group-playable game

- [ ] Choose one short adventure/ruleset, player-count range, session length, and synchronous or asynchronous turn model. A small cooperative expedition is a candidate, not a selected setting.
- [ ] Specify how players join, submit actions, discuss, take private actions, pause/resume, or become inactive. Decide deadlines, conflict resolution, and rejoining behavior before implementation.
- [ ] Define success and failure conditions, permitted actions, meaningful branches, and NPC scope. Do not expand the first slice into a complete D&D/Pathfinder implementation or generic RPG language.
- [ ] Produce a compact scenario and synthetic playthrough with multiple human-controlled characters and a recurring NPC interaction. Decide whether to run a human-hosted rehearsal before building the narrator.

### R3 — Make consulting the game mandatory, not optional tool etiquette

- [ ] Specify a host-controlled sequence: load the current permitted scene and revision; interpret the player's proposal; validate/resolve through the engine; commit; narrate the returned outcome.
- [ ] An available tool or an instruction to consult state is insufficient. No successful action outcome may be published merely because the model chose to answer without a verified engine result.
- [ ] Distinguish an observation-only request from a state-changing action. Read-only descriptions still require a current allowed projection; they do not require inventing a state-changing event.
- [ ] Bind output to session, actor/audience, revision, and the relevant committed event or read snapshot. Handle missing results, stale revisions, duplicate delivery, simultaneous actions, and restart/recovery explicitly.
- [ ] Compare constraints on factual output, allowed decorative material, validation, and safe fallback. A receipt proves which event was used, not that every sentence of free prose is faithful. Test unsupported entities, injuries, clues, numbers, and claimed outcomes separately.
- [ ] Freeze or version the ruleset per session; record random outcomes and any model-selected NPC actions needed for replay. Low temperature and a fixed prompt are not substitutes for deterministic rules.

Output: a small engine/host/narrator authority contract and failure matrix. Do not treat a successful model demonstration as a guarantee that rules cannot be bypassed.

### R4 — Persistent characters, NPCs, and relationships

- [ ] Define stable entity identities and records for characters, NPCs, locations, inventory, state, knowledge, and relevant past interactions.
- [ ] Distinguish the GM's canonical record, a player's own dossier, public observations, and another NPC's knowledge. Internal notes must not leak through character pages or context views.
- [ ] Evaluate a relationship graph as a logical data model: alliances, obligations, trust, rivalry, and other scenario-relevant links. Specify direction, provenance, change history, and who knows each fact; keep beliefs separate from world truth.
- [ ] Compare ordinary relational tables and bounded graph queries before introducing a dedicated graph database. A graph diagram alone does not solve memory or context selection.
- [ ] Specify how NPC decisions and relationship changes become validated events. NPC roleplay must not silently create items, rewrite history, or update relationships only in prose.

Output: minimal records and a worked example where an NPC remembers an earlier interaction after leaving and returning to the scene.

### R5 — Keep context bounded without losing important facts

- [ ] Build context from current scene, permitted participants, relevant relationship links, unresolved commitments, and bounded recent events. Retrieve relevant lore; do not load the entire world bible or transcript every turn.
- [ ] Keep canonical facts and event history outside lossy narrative summaries. Compression may shorten presentation but must not erase identity, unresolved promises, changes of allegiance, inventory ownership, or scene-critical discoveries.
- [ ] Test retrieval after long gaps, similarly named NPCs, corrected/superseded facts, scene transitions, contradictory beliefs, and repeated summaries.
- [ ] Measure input/output tokens, retrieval overhead, latency, and cost across short and extended sessions. Choose budgets and retention rules before paid testing. Do not assume future models will automatically become cheap enough or reliable enough.

Output: context-assembly rules, compression/rehydration tests, and a cost/latency report with measured and estimated values distinguished.

### R6 — Useful narration, private information, and interface

- [ ] Preserve the sensory narrative baseline and test noir against the same underlying scene without changing facts or revealed information. Do not impose thoughts, emotions, or choices on players.
- [ ] Give understandable physical consequences without exact HP, damage, shield absorption, or numeric combat summaries. Do not infer a fracture or fatal diagnosis solely from a health scalar.
- [ ] Treat medical assessment as an optional research branch: what is estimated, by what examination, at what uncertainty, and for which audience. It is not an exact-state inspection tool.
- [ ] Compare a shared scene card, semantic character dossiers, inline actions, and private replies. Recheck current Telegram/library permissions and limits; a richer Mini App is not automatically required for character pages.
- [ ] Clearly distinguish scenery from actionable objects and confirmed outcomes. Keep descriptions short when participants want to act, and allow richer prose for important scenes.
- [ ] Evaluate spotlight sharing, long waits, contradictory simultaneous input, and out-of-game discussion. A correct solo narrator is not sufficient acceptance for the group experience.

Output: representative player/GM views, a short action cycle, and a narrative rubric. Preserve existing ordinary-chat silence and invocation rules.

### R7 — Evaluation and first implementation admission

- [ ] Freeze a small synthetic scenario, ruleset, test fixtures, and acceptance criteria before evaluating candidates.
- [ ] Verify replay of mechanical state; invalid/forged/stale/repeated actions must not mutate it. Include concurrent actions and recovery without duplicate effects.
- [ ] Try to make the narrator bypass the engine, invent a roll/result, reveal private NPC facts or numerical state, or publish success after tool failure. All observed failures block admission until resolved or the design is narrowed.
- [ ] Test long-session continuity and the return of an NPC with a prior promise/relationship change. Confirm that public and private views remain different after compression and resume.
- [ ] Conduct a separately authorized group pilot. Collect feedback on agency, comprehensibility of condition, NPC consistency, waiting time, style, and cost. Willingness to test is not permission to send messages or start a game.
- [ ] Record limitations and remaining risks. Passing a finite test set does not prove arbitrary free text can never hallucinate; the admitted narrative surface and fallback must be explicit.

## Proposed implementation sequence — gated, not started

| Slice | Scope | Entry / exit condition |
| --- | --- | --- |
| I1 — Mechanical core | Isolated session state, ruleset version, characters/NPCs, validated actions, recorded randomness, committed events, replay | Enter after R2/R3 contracts and approval. Exit with deterministic mechanical tests; no AI narrator needed. |
| I2 — State access and memory | Allowed scene projections, semantic character dossiers, minimal relationship records, bounded context assembly | Enter after R4/R5 decisions. Exit with private-information, continuity, and compression tests. |
| I3 — Grounded narrator | Host-enforced resolution sequence, permitted observations, sensory style, factual checks and fallback | Enter with I1/I2 contracts. Exit with adversarial outcome/leakage checks and narrative review. |
| I4 — Group-chat integration | Explicit game invocation, participant/turn handling, shared/private delivery, duplicate protection, pause/resume | Enter after R2/R6 interface decisions. Exit with multiplayer, concurrency, delivery, and ordinary-chat isolation checks. |
| I5 — Pilot and release decision | Authorized group rehearsal, long-session evaluation, cost/latency review, scoped hardening | Enter only after technical gates and pilot approval. Merge/deploy remain separate authorizations and repository release gates. |

Adjust the slice boundaries after research rather than creating speculative implementation issues now. Each admitted slice needs an owner, concrete acceptance tests, non-goals, and a privacy review.

## Non-goals and unselected decisions

- No implementation, deployment, provider/model change, paid benchmark, live game, or Telegram message is authorized by recording this plan.
- No full commercial platform, Steam release, monetization plan, automatic imports of real participants' personal data, or migration of ordinary chat memory into fiction.
- No claim that a full lore book makes a model a reliable GM; no claim that tools alone enforce their own use.
- No forced linear plot, universal noir tone, mandatory graph database, or numerical character sheet for players.
- D&D is the interaction reference; its edition, the adventure setting, number of players, session timing, actor-specific input format, physician mechanics, context budget, and reuse/build choice remain open.

## Next concrete step and completion condition

Start with R1 and R2: finish the candidate matrix and choose a small group scenario to make the remaining contracts concrete. Then resolve R3–R6 and freeze R7 acceptance before opening implementation slices.

Research is complete when the reuse/build decision, minimal ruleset, state/knowledge ownership, context policy, player views, budget envelope, and acceptance fixtures are documented with unresolved blockers explicitly named. This plan remains Todo until research execution is selected.

Related repository work: [Telegram API capability research #80](https://github.com/Turkevich91/Aigan/issues/80) and [field-test tooling #159](https://github.com/Turkevich91/Aigan/issues/159). They are adjacent references, not replacements for this game plan or automatic prerequisites.
