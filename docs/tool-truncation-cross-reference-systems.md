# Tool Truncation Across Reference Systems

A comparison of how the five reference systems handle large tool results.

---

## OpenCode

**File:** `packages/opencode/src/tool/truncate.ts`

### Strategy: Dedicated truncation service with head/tail slicing + file persistence

**Limits:**
- `MAX_LINES = 2000`
- `MAX_BYTES = 50 KB`
- Both configurable via `tool_output.max_lines` / `tool_output.max_bytes` in config

**Core logic (`output()` function):**
1. If content fits both limits → return as-is
2. Otherwise slice from **head** (default) or **tail** based on `direction` option
3. Writes full original to a timestamped file in `TRUNCATION_DIR/` (e.g. `tool_20260101_001423.txt`)
4. Returns preview with a hint

**Hint differs by agent capability:**
- Has `task` tool → `"Use the Task tool to have explore agent process this file with Grep and Read. Do NOT read the full file yourself."`
- Otherwise → `"Use Grep to search the full content or Read with offset/limit."`

**Cleanup:** Runs hourly via `Effect.repeat(Schedule.spaced(Duration.hours(1)))`, deletes files older than 7 days (`RETENTION = Duration.days(7)`).

**Return type:**
```typescript
type Result =
  | { content: string; truncated: false }
  | { content: string; truncated: true; outputPath: string }
```

**Key design:** Effect-based (functional) architecture with dependency injection. Truncation is a first-class service with its own lifecycle.

---

## Hermes Agent

**Files:** `tools/tool_output_limits.py`, `tools/tool_result_storage.py`

### Strategy: Three-layer defense

**Layer 1 — Per-tool caps:**
Tools like `terminal_tool.py` (`MAX_OUTPUT_CHARS = 50_000`) and `file_operations.py` (`MAX_LINES = 2000`, `MAX_LINE_LENGTH = 2000`) truncate their own output before returning.

**Layer 2 — Per-result persistence (`maybe_persist_tool_result`):**
After a tool returns, if output exceeds the tool's registered threshold (`registry.get_max_result_size`), the full output is written to `/tmp/hermes-results/{tool_use_id}.txt` via `env.execute()` (so it's accessible from any backend: local, Docker, SSH, Modal, Daytona). The in-context content is replaced with a `<persisted-output>` preview + file path.

Writes via stdin to avoid Linux's `MAX_ARG_STRLEN` (~128 KB) ceiling on command arguments:
```python
cmd = f"mkdir -p {shlex.quote(storage_dir)} && cat > {shlex.quote(remote_path)}"
result = env.execute(cmd, timeout=30, stdin_data=content)
```

**Layer 3 — Per-turn aggregate budget (`enforce_turn_budget`):**
After all tool results in a single assistant turn are collected, if total exceeds `MAX_TURN_BUDGET_CHARS` (200K), the largest non-persisted results are spilled to disk until under budget. Catches cases where many medium-sized results combine to overflow context.

**Limits (configurable via `config.yaml` `tool_output` section):**
- `DEFAULT_MAX_BYTES = 50_000`
- `DEFAULT_MAX_LINES = 2000`
- `DEFAULT_MAX_LINE_LENGTH = 2000`

**Preview generation:**
```python
def generate_preview(content: str, max_chars: int = DEFAULT_PREVIEW_SIZE_CHARS) -> tuple[str, bool]:
    if len(content) <= max_chars:
        return content, False
    truncated = content[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars // 2:
        truncated = truncated[:last_nl + 1]
    return truncated, True
```

**Persistence directory:** Resolves dynamically via `env.get_temp_dir()` falling back to `/tmp/hermes-results`.

---

## cc-haha (Claude Code successor)

**Files:** `src/utils/truncate.ts`, `src/utils/toolResultStorage.ts`

### Strategy: Per-tool thresholds + message-level aggregate budget

**Per-tool persistence threshold:**
```typescript
// DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000
// Per-tool declared maxResultSizeChars is clamped by the global default
export function getPersistenceThreshold(
  toolName: string,
  declaredMaxResultSizeChars: number,
): number {
  if (!Number.isFinite(declaredMaxResultSizeChars)) {
    return declaredMaxResultSizeChars  // Infinity = opt-out (e.g. Read)
  }
  const override = GrowthBookOverrides?.[toolName]
  if (typeof override === 'number' && Number.isFinite(override) && override > 0) {
    return override
  }
  return Math.min(declaredMaxResultSizeChars, DEFAULT_MAX_RESULT_SIZE_CHARS)
}
```

**Persistence flow (`maybePersistLargeToolResult`):**
1. Empty content guard — injects `({toolName} completed with no output)` marker to prevent stop-sequence issues
2. Image blocks bypassed — sent as-is to Claude
3. Content size ≤ threshold → return unchanged
4. Otherwise `persistToolResult()` writes to `{projectDir}/{sessionId}/tool-results/{tool_use_id}.{json|txt}`
5. Returns `{ ...toolResultBlock, content: <persisted-output> message }`

**Preview generation:**
```typescript
export function generatePreview(content: string, maxBytes: number): { preview: string; hasMore: boolean } {
  if (content.length <= maxBytes) return { preview: content, hasMore: false }
  const truncated = content.slice(0, maxBytes)
  const lastNewline = truncated.lastIndexOf('\n')
  const cutPoint = lastNewline > maxBytes * 0.5 ? lastNewline : maxBytes
  return { preview: content.slice(0, cutPoint), hasMore: true }
}
```

**Aggregate budget (per-message):**
- `MAX_TOOL_RESULTS_PER_MESSAGE_CHARS` — hard cap on total tool result chars per API message
- `ContentReplacementState` tracks `seenIds: Set<string>` and `replacements: Map<string, string>` across turns for prompt cache stability
- Re-applies cached replacements from prior turns (zero I/O, byte-identical)
- Fresh results persist largest-first until under budget
- GrowthBook override flag: `tengu_hawthorn_window`

**Key design:** Strong separation between per-tool persistence and per-message aggregate budget, with explicit state management for prompt cache correctness.

---

## OpenClaw

**File:** `src/agents/pi-embedded-runner/tool-result-truncation.ts`

### Strategy: Context-share-aware truncation with head+tail preservation

**Hard cap:** `DEFAULT_MAX_LIVE_TOOL_RESULT_CHARS = 16_000` (backwards-compatible alias: `HARD_MAX_TOOL_RESULT_CHARS`)

**Context-aware dynamic cap:**
```typescript
export function calculateMaxToolResultChars(contextWindowTokens: number): number {
  const maxTokens = Math.floor(contextWindowTokens * MAX_TOOL_RESULT_CONTEXT_SHARE)
// MAX_TOOL_RESULT_CONTEXT_SHARE = 0.3
// ~4 chars per token heuristic
  return Math.min(maxTokens * 4, DEFAULT_MAX_LIVE_TOOL_RESULT_CHARS)
}
```

**Truncation strategy — head+tail when tail is important:**
```typescript
function hasImportantTail(text: string): boolean {
  const tail = text.slice(-2000)
  return (
    /\b(error|exception|failed|fatal|traceback|panic|stack trace|errno|exit code)\b/.test(tail) ||
    /\}\s*$/.test(tail.trim()) ||  // JSON closing
    /\b(total|summary|result|complete|finished|done)\b/.test(tail)
  )
}

export function truncateToolResultText(text: string, maxChars: number, options?): string {
  if (hasImportantTail(text) && budget > minKeepChars * 2) {
    const tailBudget = Math.min(Math.floor(budget * 0.3), 4_000)
    const headBudget = budget - tailBudget - MIDDLE_OMISSION_MARKER.length
    // Keep head + ...middle omitted... + tail
    const keptText = text.slice(0, headCut) + MIDDLE_OMISSION_MARKER + text.slice(tailStart)
    return appendBoundedTruncationSuffix(...)
  }
  // Default: keep beginning
}
```

**Suffix:** `formatContextLimitTruncationNotice(truncatedChars)` — structured notice about what was omitted

**Two enforcement layers:**
1. `truncateOversizedToolResultsInMessages()` — pre-emptive in-memory guard before sending to LLM (does not modify session file)
2. `truncateOversizedToolResultsInSession()` — modifies session file transcript for recovery after context window overflow

**Min keep chars:** `MIN_KEEP_CHARS = 2_000` (always keeps at least first portion so model understands context)

**Key design:** Context-window-aware (not just static limits). Head+tail strategy preserves error messages and JSON structure at the end of output.

---

## Paperclip

**File:** `server/src/services/workspace-runtime.ts`

### Strategy: Adapter-level truncation with per-message budget enforcement

Paperclip delegates truncation to adapter implementations. Each adapter (`opencode-local`, `codex-local`, `gemini-local`, `pi-local`, etc.) has its own `parseStdout.ts` that handles tool output truncation.

**Common pattern in adapters:**
```typescript
// e.g. adapters/opencode-local/src/server/parse.ts
// Truncates tool output at a fixed char limit before returning
```

The server-level `workspace-runtime.ts` orchestrates session management and defers to adapter-specific parsing. The actual truncation limits are adapter-specific rather than centralized.

**Aggregate budget:** Handled at the session transcript level via `rewriteTranscriptEntriesInSessionManager()` and `rewriteTranscriptEntriesInState()` — tool results that exceed budgets are rewritten in the transcript file.

**Key design:** Truncation is delegated to thin adapter implementations close to where tool output is produced. Server coordinates transcript-level persistence and rewriting.

---

## Summary Comparison

| System | Strategy | Limits | Persistence | Unique Feature |
|---|---|---|---|---|
| **OpenCode** | Head/tail slice + file | 2000 lines / 50 KB (configurable) | Yes — timestamped file in `TRUNCATION_DIR/` | Effect-based service with hourly cleanup |
| **Hermes Agent** | 3-layer defense: per-tool → per-result → per-turn | 50K chars / 2000 lines (configurable) | Yes — `/tmp/hermes-results/` via env.execute() | Three-tier cascade: tool cap → persist → aggregate budget |
| **cc-haha** | Per-tool threshold + message-level budget | 50K default (GrowthBook-overridable per tool) | Yes — `{projectDir}/{sessionId}/tool-results/` | `ContentReplacementState` for prompt cache stability; GrowthBook overrides |
| **OpenClaw** | Context-share-aware head+tail | 16K default, scales with context window (30% share) | No file persistence | Head+tail preserves errors/JSON at end; context-window-aware |
| **Paperclip** | Adapter-level + transcript rewrite | Adapter-specific | Adapter-specific | Delegate to thin adapters; server coordinates session-level rewrite |

### Key Themes

1. **File persistence** is the dominant approach for truly large outputs — write to disk, return preview + pointer (OpenCode, Hermes Agent, cc-haha). OpenClaw opts for inline truncation only.

2. **Configurable limits** are universal — every system lets operators tune thresholds without patching source.

3. **Aggregate budgets** appear in Hermes Agent and cc-haha to handle the case where many medium-sized results combine to overflow context.

4. **Context-window awareness** — only OpenClaw dynamically scales its limit based on the model's context window size and a 30% share heuristic.

5. **Prompt cache stability** — cc-haha explicitly tracks replacement state across turns so re-applied previews are byte-identical, preserving cache hits.

6. **Error preservation** — OpenClaw's head+tail strategy specifically detects error-like patterns in the tail and preserves them, which other systems don't do.