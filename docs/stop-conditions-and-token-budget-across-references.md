# Stop Conditions & Token/Cost Budget Across 5 Reference Systems

How each system handles agent loop termination — graceful wrap-up, token budgets, step caps, and cost limits.

---

## 1. OpenClaw (TypeScript)

**Grace Call / Stop Mechanism**

OpenClaw's primary stop mechanism is **session suspension** (`session-suspension.ts`):

```typescript
export type SessionSuspensionReason = "quota_exhausted" | "manual" | "circuit_open";

export function resolveSessionSuspensionReason(reason: FailoverReason): SessionSuspensionReason {
  if (reason === "billing") return "manual";
  if (reason === "rate_limit") return "quota_exhausted";
  return "circuit_open";
}
```

A session is suspended for `DEFAULT_QUOTA_SUSPENSION_RESUME_MS = 30 minutes` when billing errors or rate limits fire. The lane's command concurrency is set to `0` during suspension, blocking new work.

For graceful wrap-up within a turn, OpenClaw relies on **tool result truncation** (`tool-result-truncation.ts`) and **compaction** — there is no explicit one-turn-ahead grace call injection like the course describes. The system detects context window pressure and triggers compaction before the loop would naturally stop.

**Token / Cost Cap**

Tool result truncation is context-share-aware:

```typescript
const MAX_TOOL_RESULT_CONTEXT_SHARE = 0.30;
export const DEFAULT_MAX_LIVE_TOOL_RESULT_CHARS = 16_000;

export function calculateMaxToolResultChars(contextWindowTokens: number): number {
  const maxTokens = Math.floor(contextWindowTokens * MAX_TOOL_RESULT_CONTEXT_SHARE);
  const maxChars = maxTokens * 4;  // ~4 chars per token heuristic
  return Math.min(maxChars, Math.max(1, hardCapChars));
}
```

**Head+tail truncation** when tail is important:
- Checks last 2000 chars for error keywords, JSON closing `}`, or summary keywords (total, summary, result, complete, finished, done)
- If tail is important and content exceeds `1.5×` the cap: preserves head (~70%) + tail (~30%, up to 4000 chars)
- Otherwise: head-only with suffix note

No file persistence for truncated results — the truncation is in-memory only and does not spill to disk.

**Three paths** at step boundary: OpenClaw's compaction system handles the continue/stop/compact decision. Compaction is triggered when context window fills; it produces a summary and replaces the message array.

---

## 2. cc-haha / Claude Code successor (TypeScript)

**Grace Call / Stop Mechanism**

cc-haha uses a dedicated **stop hook system** (`src/query/stopHooks.ts`):

```typescript
export async function* handleStopHooks(
  messagesForQuery: Message[],
  assistantMessages: AssistantMessage[],
  systemPrompt: SystemPrompt,
  userContext: { [k: string]: string },
  systemContext: { [k: string]: string },
  toolUseContext: ToolUseContext,
  querySource: QuerySource,
  stopHookActive?: boolean,
): AsyncGenerator<StreamEvent | RequestStartEvent | Message | TombstoneMessage | ToolUseSummaryMessage, StopHookResult>
```

The return value is a `StopHookResult`:
```typescript
type StopHookResult = {
  blockingErrors: Message[]
  preventContinuation: boolean
}
```

`preventContinuation: true` → agent loop stops. `blockingErrors` can hold user-facing messages explaining why.

Additionally, `src/utils/gracefulShutdown.ts` handles process-level graceful shutdown via signals (SIGINT, SIGTERM, SIGHUP), with a failsafe timer that guarantees exit even if cleanup hangs.

**Token / Cost Cap**

`src/utils/tokenBudget.ts` — `checkTokenBudget()` function:

```typescript
const COMPLETION_THRESHOLD = 0.9   // 90% of budget
const DIMINISHING_THRESHOLD = 500  // tokens of progress per turn

export function checkTokenBudget(
  tracker: BudgetTracker,
  agentId: string | undefined,
  budget: number | null,
  globalTurnTokens: number,
): TokenBudgetDecision {
  if (agentId || budget === null || budget <= 0) {
    return { action: 'stop', completionEvent: null }
  }

  const turnTokens = globalTurnTokens
  const pct = Math.round((turnTokens / budget) * 100)
  const deltaSinceLastCheck = globalTurnTokens - tracker.lastGlobalTurnTokens

  const isDiminishing =
    tracker.continuationCount >= 3 &&
    deltaSinceLastCheck < DIMINISHING_THRESHOLD &&
    tracker.lastDeltaTokens < DIMINISHING_THRESHOLD

  if (!isDiminishing && turnTokens < budget * COMPLETION_THRESHOLD) {
    tracker.continuationCount++
    tracker.lastDeltaTokens = deltaSinceLastCheck
    tracker.lastGlobalTurnTokens = globalTurnTokens
    return { action: 'continue', nudgeMessage: getBudgetContinuationMessage(pct, turnTokens, budget), ... }
  }

  if (isDiminishing || tracker.continuationCount > 0) {
    return { action: 'stop', completionEvent: { diminishingReturns: isDiminishing, ... } }
  }

  return { action: 'stop', completionEvent: null }
}
```

The continuation message when budget is not yet exhausted:
```typescript
export function getBudgetContinuationMessage(pct: number, turnTokens: number, budget: number): string {
  return `Stopped at ${pct}% of token target (${fmt(turnTokens)} / ${fmt(budget)}). Keep working — do not summarize.`
}
```

**Summary**: cc-haha uses diminishing returns detection (3+ consecutive turns with <500 token progress) as the primary stop trigger, not a hard step cap. The `COMPLETION_THRESHOLD = 0.9` means the stop fires when 90% of the budget is reached if diminishing returns are also detected. Stop hooks are the extensibility point for custom stop logic.

---

## 3. OpenCode (TypeScript)

**Grace Call / Stop Mechanism**

OpenCode does not have an explicit "grace call" in the course's sense of "inject a wrap-up hint one turn before budget exhaustion." Instead, it relies on **compaction** as the primary mechanism for handling resource limits. The loop runs until compaction is triggered or the model returns no tool calls.

Stop detection is built into the `runEmbeddedPiAgent` loop in `run.ts`. The key stop signals are:
- Model returns no `tool_use` blocks → loop exits naturally
- Compaction fires when context window fills → summary replaces history, loop continues
- Retry/exhaustion limits on individual attempts

**Token / Cost Cap**

OpenCode's truncation (`tool/truncate.ts`) is a service (Effect-based, dependency-injected):

```typescript
export class TruncateService {
  constructor(private config: { maxBytes: number; maxLines: number }) {}

  truncate(text: string, direction: 'head' | 'tail' = 'head'): { content: string; truncated: boolean; outputPath?: string }
}
```

Limits: `MAX_LINES = 2000`, `MAX_BYTES = 50 KB` (configurable). Persists full output to a timestamped file in `TRUNCATION_DIR/`, returns preview + pointer.

Cleanup runs hourly via `Effect.repeat(Schedule.spaced(Duration.hours(1)))`, deleting files older than 7 days (`RETENTION = Duration.days(7)`).

**No grace call** — OpenCode relies entirely on compaction summaries when context runs low. There is no budget-exhaustion injection into the system prompt.

---

## 4. Hermes Agent (Python)

**Grace Call / Stop Mechanism**

Hermes Agent uses a **layered output limit system** (`tools/tool_output_limits.py`):

Layer 1 — Per-tool caps:
```python
# terminal_tool.py
MAX_OUTPUT_CHARS = 50_000

# file_operations.py
MAX_LINES = 2000
MAX_LINE_LENGTH = 2000
```

Layer 2 — Per-result persistence (`tools/tool_result_storage.py`):
```python
def maybe_persist_tool_result(tool_use_id: str, output: str) -> str:
    # If output > threshold, write to /tmp/hermes-results/{tool_use_id}.txt
    # Return <persisted-output> preview + file path
    pass
```

Layer 3 — Per-turn aggregate budget (`enforce_turn_budget()`):
```python
MAX_TURN_BUDGET_CHARS = 200_000  # after all tool results in one turn
# If total exceeds budget, spill largest non-persisted results to disk
```

**No explicit grace call** — Hermes Agent's "wrap up" behavior is implicit: when a tool result would exceed its cap, it is truncated or persisted. The model sees a truncated preview and must work with what remains.

**Token / Cost Cap**: No explicit token budget enforcement; per-tool character caps and per-turn aggregate budgets are the control knobs.

---

## 5. Paperclip (Python)

**Grace Call / Stop Mechanism**

Paperclip delegates stop condition handling to **adapters** — each channel/connector adapter implements its own truncation logic. The server coordinates via `rewriteTranscriptEntriesInSessionManager()` (`controller.py`) for transcript-level rewriting.

```python
# Controller coordinates transcript rewriting across adapters
rewriteTranscriptEntriesInSessionManager()
```

**Token / Cost Cap**: Adapter-level. No centralized token budget — each adapter handles its own limits. The server's role is transcript state management and governance coordination across agents.

---

## Cross-Cutting Summary

| System | Grace Call? | Token/Cost Cap | Step Cap | Truncation Strategy |
|---|---|---|---|---|
| **OpenClaw** | No explicit grace call — session suspension on billing/rate_limit errors; compaction before natural stop | Context-share-aware: 30% of context window, ~16K chars default | Not explicitly exposed as a config | Head+tail with `hasImportantTail()` detection; no file persistence |
| **cc-haha** | Stop hooks with `preventContinuation` flag; diminishing returns detection after 3+ turns of <500 token progress | `COMPLETION_THRESHOLD = 0.9` of budget + diminishing returns check | Not a primary mechanism — diminishing returns handles it | Persists large results to `{projectDir}/{sessionId}/tool-results/`; GrowthBook overrides per tool |
| **OpenCode** | No grace call — compaction fires when context fills | 50 KB / 2000 lines hard cap per tool result | Not explicitly exposed | Head or tail slice + timestamped file persistence; hourly cleanup of files > 7 days old |
| **Hermes Agent** | No grace call — implicit via per-tool/per-turn caps | 50K chars per tool / 2000 lines / 200K per turn aggregate | Not explicitly exposed | 3-layer: per-tool cap → per-result persistence → per-turn spill to disk |
| **Paperclip** | No grace call — adapter-level delegation | Adapter-level — no centralized budget | Not explicitly exposed | Adapter-level truncation; server coordinates transcript rewrite |

**Key theme**: Only the course's Ch.02 design (and cc-haha with its `getBudgetContinuationMessage`) explicitly describe a **grace call** mechanism — one turn of warning before forcing a stop. The reference systems mostly handle resource limits through truncation or compaction, not through system-prompt injection of a wrap-up hint.

**Three paths** (continue / stop / compact) are implemented by:
- **OpenClaw**: Compaction system
- **OpenCode**: Compaction summaries
- **cc-haha**: `checkTokenBudget` returns `continue` or `stop`; `decideAtStepBoundary` in loop hooks
- **Hermes Agent**: Turn budget enforcement + compaction summaries
- **Paperclip**: Adapter-level; no universal three-path mechanism

---

## Human-Readable Translation

| Implementation | What it actually does |
|---|---|
| OpenClaw's `session-suspension` | "If you hit a billing error or rate limit, freeze the session for 30 minutes and stop accepting new work." |
| cc-haha's `checkTokenBudget` with diminishing returns | "After 3 turns where you made less than 500 tokens of progress, stop — you're in a diminishing-returns loop." |
| cc-haha's `getBudgetContinuationMessage` | "You're at 67% of your token budget. Keep working, but don't summarize — we need to use the remaining tokens efficiently." |
| OpenCode's `TruncateService` | "If a tool result exceeds 50KB or 2000 lines, cut it down and save the full version to a timestamped file so you can read it later if needed." |
| Hermes Agent's 3-layer limits | "Every tool can output 50K chars max. If a single tool result is bigger, save it to disk. If all tool results in one turn total more than 200K, save the biggest ones to disk too." |
| OpenClaw's context-share-aware cap | "Never let a single tool result eat more than 30% of the context window. If the context window is 128K tokens, that's about 16K characters for one tool result." |
| OpenClaw's `hasImportantTail()` | "If the tool output ends with errors, JSON closing, or summary words, preserve the tail when truncating — the most important stuff is at the end." |