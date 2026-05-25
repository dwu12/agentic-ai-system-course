"""
agent_utils.py — Reusable functions from Ch.01 (One Tool Call)

Import this module in notebooks to get:
    - send_messages()          : send a message list to the model
    - process_response()       : parse response content blocks
    - execute_single_tool()   : validate + execute one tool call
    - execute_all_tools_parallel() : run multiple tools concurrently
    - run_single_turn()        : end-to-end single turn helper
    - truncate_result()       : OpenClaw + OpenCode truncation pipeline

Prerequisites:
    from anthropic import Anthropic
    from dotenv import load_dotenv
    load_dotenv()
    client = Anthropic()
"""

import json
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

# ----------------------------------------------------------------------
# Core client
# ----------------------------------------------------------------------

def get_client():
    """Returns a cached Anthropic client. Safe to call multiple times."""
    from anthropic import Anthropic
    from dotenv import load_dotenv
    load_dotenv()
    return Anthropic()

# ----------------------------------------------------------------------
# Message building
# ----------------------------------------------------------------------

def build_user_message(content: str) -> dict:
    """Single user message dict."""
    return {"role": "user", "content": content}


def build_assistant_message(content: list) -> dict:
    """
    Assistant message with a list of content blocks
    (e.g. thinking + text + tool_use blocks from a response).
    """
    return {"role": "assistant", "content": content}


def build_tool_result_message(tool_use_id: str, content: str) -> dict:
    """Single tool_result block as a user message."""
    return {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content
        }]
    }

# ----------------------------------------------------------------------
# Core API call
# ----------------------------------------------------------------------

def send_messages(messages, tools: Optional[list] = None, model: str = "MiniMax-M2.7", max_tokens: int = 4096):
    """
    Send a message list to the model and return the response.
    """
    client = get_client()
    params = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if tools:
        params["tools"] = tools
    return client.messages.create(**params)


# ----------------------------------------------------------------------
# Response parsing
# ----------------------------------------------------------------------

def process_response(response, verbose: bool = True):
    """
    Parse a model response into three lists: thinking_blocks, text_blocks, tool_use_blocks.
    Prints them if verbose=True.

    Returns:
        (thinking_blocks, text_blocks, tool_use_blocks)
    """
    thinking_blocks = []
    text_blocks = []
    tool_use_blocks = []

    for block in response.content:
        if block.type == "thinking":
            thinking_blocks.append(block)
            if verbose:
                print(f"💭 Thinking>\n{block.thinking}\n")
        elif block.type == "text":
            text_blocks.append(block)
            if verbose:
                print(f"💬 Model>\t{block.text}")
        elif block.type == "tool_use":
            tool_use_blocks.append(block)
            if verbose:
                print(f"🔧 Tool>\t{block.name}({json.dumps(block.input, ensure_ascii=False)})")

    return thinking_blocks, text_blocks, tool_use_blocks


# ----------------------------------------------------------------------
# Tool execution (single + parallel)
# ----------------------------------------------------------------------

def execute_single_tool(tool_call, tool_handlers: dict, tools: Optional[list] = None):
    """
    Execute a single tool call: validate → run handler → return tool_result dict.
    Never raises — errors become readable tool_result content.

    Args:
        tool_call     : tool_use block from the model response
        tool_handlers : {tool_name: handler_function} dict
        tools         : optional list of tool schemas for validation

    Returns:
        {"tool_use_id": ..., "content": ...}
    """
    tool_id = tool_call.id
    tool_name = tool_call.name
    args = tool_call.input

    # Validate: tool must exist in registry
    if tool_name not in tool_handlers:
        return {"tool_use_id": tool_id, "content": f"Unknown tool: {tool_name}"}

    # Validate: required fields from schema
    if tools:
        tool_schema = next((t for t in tools if t["name"] == tool_name), None)
        if tool_schema:
            required = tool_schema["input_schema"].get("required", [])
            for field in required:
                if field not in args:
                    return {"tool_use_id": tool_id, "content": f"Validation error: missing required field '{field}'"}

    # Execute handler
    try:
        handler = tool_handlers[tool_name]
        result = handler(**args)
        return {"tool_use_id": tool_id, "content": result}
    except Exception as e:
        return {"tool_use_id": tool_id, "content": f"Execution error: {str(e)}"}


def execute_all_tools_parallel(tool_calls, tool_handlers: dict, tools: Optional[list] = None):
    """
    Execute multiple tool calls concurrently, preserving original order.

    Args:
        tool_calls     : list of tool_use blocks
        tool_handlers  : {tool_name: handler_function} dict
        tools          : optional list of tool schemas for validation

    Returns:
        list of {"tool_use_id": ..., "content": ...} dicts, in same order as tool_calls
    """
    results = [None] * len(tool_calls)

    with ThreadPoolExecutor(max_workers=len(tool_calls)) as executor:
        future_to_index = {
            executor.submit(execute_single_tool, tc, tool_handlers, tools): i
            for i, tc in enumerate(tool_calls)
        }
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            results[idx] = future.result()

    return results


# ----------------------------------------------------------------------
# End-to-end single turn
# ----------------------------------------------------------------------

def run_single_turn(
    user_message: str,
    tools: Optional[list] = None,
    tool_handlers: Optional[dict] = None,
    model: str = "MiniMax-M2.7",
    max_tokens: int = 4096,
    verbose: bool = True,
):
    """
    Full single-turn with parallel tool support:

    1. Send user message → model
    2. Extract all tool_use blocks (0, 1, or many)
    3. Execute all in parallel using ThreadPoolExecutor
    4. Feed all tool_result messages back to model
    5. Return final response

    Args:
        user_message   : string — the user's prompt
        tools          : list of tool schemas (for validation)
        tool_handlers  : {tool_name: handler_function} dict
        model          : model name string
        max_tokens     : max tokens to return
        verbose        : if True, print thinking/text/tool blocks

    Returns:
        model response object
    """
    if tool_handlers is None:
        tool_handlers = {}

    messages = [{"role": "user", "content": user_message}]

    # Step 1: get model response
    response = send_messages(messages, tools=tools, model=model, max_tokens=max_tokens)

    # Step 2: collect all tool_use blocks
    tool_calls = [b for b in response.content if b.type == "tool_use"]

    if not tool_calls:
        if verbose:
            process_response(response, verbose=True)
        return response

    # Step 3: append full response.content to history
    messages.append({"role": "assistant", "content": response.content})

    # Step 4: execute all tools in parallel
    all_results = execute_all_tools_parallel(tool_calls, tool_handlers, tools)

    # Step 5: append each tool result to history
    for result in all_results:
        messages.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": result["tool_use_id"],
                "content": result["content"]
            }]
        })

    # Step 6: get final response
    final_response = send_messages(messages, tools=tools, model=model, max_tokens=max_tokens)

    if verbose:
        process_response(final_response, verbose=True)

    return final_response


# ----------------------------------------------------------------------
# Truncation — OpenClaw + OpenCode combined strategy
# ----------------------------------------------------------------------
# OpenClaw contribution:
#   - Context-share-aware max chars: min(DEFAULT_MAX_CHARS, context_window * 0.3 * 4)
#   - hasImportantTail() — detect errors/JSON/summaries at the end
#   - Head+tail truncation when tail is important
#
# OpenCode contribution:
#   - File persistence: write full result to timestamped temp file
#   - Return preview + pointer + hint to use Read/Grep
# ----------------------------------------------------------------------

DEFAULT_MAX_CHARS = 50_000   # 50 KB hard cap
MIN_KEEP_CHARS    = 2_000     # always keep at least first 2K chars
CONTEXT_SHARE     = 0.30      # max 30% of context window per tool result
CHARS_PER_TOKEN   = 4         # rough heuristic

# Module-level truncation dir — created once per import
_TRUNCATION_DIR = tempfile.mkdtemp(prefix="tool_result_")


def _timestamp_id() -> str:
    """Unique timestamp-based ID for persisted files."""
    return f"tool_{int(time.time() * 1_000_000):016d}"


def _has_important_tail(text: str) -> bool:
    """
    OpenClaw pattern: detect whether the tail contains important content
    (errors, JSON closing, summary keywords) that should be preserved.
    """
    tail = text[-2000:].lower()
    return (
        bool(re.search(r'\b(error|exception|failed|fatal|traceback|panic|stack trace|errno|exit code)\b', tail)) or
        bool(re.search(r'^\s*\}', tail)) or
        bool(re.search(r'\b(total|summary|result|complete|finished|done)\b', tail))
    )


def _find_newline_cut(text: str, target: int) -> int:
    """Find a newline near target to avoid cutting mid-line."""
    nl = text.rfind('\n', 0, target)
    if nl > target * 0.8:
        return nl
    return target


def _truncate_head_tail(text: str, max_chars: int, min_keep: int) -> tuple:
    """
    OpenClaw pattern: preserve head + tail when tail is important.
    Returns (kept_text, omitted_count).
    """
    suffix_note = f"\n\n... {len(text) - max_chars} chars omitted ...\n"
    available = max_chars - len(suffix_note)
    tail_budget = min(available * 0.30, 4_000)
    head_budget = available - tail_budget - 50

    if head_budget <= min_keep:
        cut = _find_newline_cut(text, head_budget)
        return text[:cut] + suffix_note, len(text) - head_budget

    head_cut = _find_newline_cut(text, head_budget)
    tail_start = max(0, len(text) - int(tail_budget))
    tail_nl = text.find('\n', tail_start)
    if tail_nl != -1 and tail_nl < tail_start + tail_budget * 0.2:
        tail_start = tail_nl + 1

    head = text[:head_cut]
    tail = text[tail_start:]
    middle = f"\n\n... {len(text) - len(head) - len(tail)} chars omitted ...\n\n"
    return head + middle + tail, len(text) - (len(head) + len(tail))


def truncate_result(text: str, context_window_tokens: int = 128_000) -> dict:
    """
    Full truncation pipeline: context-share-aware + head+tail + file persistence.

    1. Compute effective max chars: min(DEFAULT_MAX_CHARS, context_share * tokens)
    2. If content fits → return as-is
    3. If tail is important → head+tail preservation
    4. Else → head + suffix note
    5. Write full original to temp file; return preview + pointer

    Returns:
        {
            "content": truncated or original text,
            "truncated": bool,
            "output_path": str (only if truncated),
            "original_size": int,
            "omitted_chars": int (only if truncated),
        }
    """
    # Step 1: context-share-aware cap
    context_max = int(context_window_tokens * CONTEXT_SHARE * CHARS_PER_TOKEN)
    max_chars = min(DEFAULT_MAX_CHARS, context_max)

    if len(text) <= max_chars:
        return {"content": text, "truncated": False}

    # Step 2: check if tail is important
    if _has_important_tail(text) and len(text) > max_chars * 1.5:
        kept, omitted = _truncate_head_tail(text, max_chars, MIN_KEEP_CHARS)
    else:
        cut = _find_newline_cut(text, max_chars - 200)
        kept = text[:cut]
        omitted = len(text) - len(kept)
        suffix_note = f"\n\n... {omitted} chars truncated ...\n"
        kept = kept + suffix_note

    # Step 3: persist full result to file
    file_id = _timestamp_id()
    file_path = f"{_TRUNCATION_DIR}/{file_id}.txt"
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(text)

    # Step 4: build final content with pointer hint
    hint = (
        f"\n\n[Tool result truncated: {len(text):,} total chars, "
        f"{omitted} omitted. Full output saved to: {file_path}. "
        f"Use the Read tool with offset/limit to access specific sections, "
        f"or Grep to search the full content.]"
    )

    final_content = kept + hint
    if len(final_content) > max_chars:
        final_content = final_content[:max_chars - len(hint)] + hint

    return {
        "content": final_content,
        "truncated": True,
        "output_path": file_path,
        "original_size": len(text),
        "omitted_chars": omitted if isinstance(omitted, int) else len(text) - len(kept),
    }


def get_truncation_dir() -> str:
    """Returns the module's truncation directory path."""
    return _TRUNCATION_DIR