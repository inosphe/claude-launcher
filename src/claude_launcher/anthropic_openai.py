"""Anthropic Messages <-> OpenAI Chat Completions, for the routing shim.

Some backends serve a model only on the OpenAI protocol (Command Code's
``deepseek/deepseek-v4.1-flash`` answers ``/provider/v1/chat/completions``
and refuses it on ``/provider/messages``), while Claude Code speaks nothing
but Anthropic Messages. A provider that declares
``anthropic_translation: openai`` (read by :func:`routing.anthropic_translation`)
has its claude sessions fronted by a shim whose upstream is the provider's
``endpoints.openai`` base; the shim hands a ``/v1/messages`` request to this
module, sends the result to ``<base>/chat/completions``, and turns the answer
back into the Messages shape, streamed or not.

Pure functions plus one stream state machine (:class:`StreamTranslator`), so
the whole mapping is testable without a socket. What is mapped:

request   ``system`` -> a system message; text / image / tool_use /
          tool_result / thinking blocks -> chat messages (``tool_result`` ->
          ``role: tool``); ``tools`` -> function tools (server tools without
          an ``input_schema`` are dropped); ``tool_choice``;
          ``max_tokens``; ``stop_sequences`` -> ``stop``; ``temperature``;
          ``top_p``; ``thinking`` -> the provider's reasoning encoding
          (``openai_reasoning_format``); ``stream`` with usage requested.
response  ``content`` -> a text block, ``reasoning_content`` (or
          ``reasoning``) -> a thinking block, ``tool_calls`` -> tool_use
          blocks, ``finish_reason`` -> ``stop_reason``, ``usage`` -> input /
          output / cache-read tokens; an upstream error body -> an Anthropic
          error body.

Not mapped: ``top_k``, ``cache_control`` (dropped), ``metadata``, documents
other than plain text (replaced by a placeholder line).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Iterable, List, Optional

#: The one target protocol a provider may name in ``anthropic_translation``.
OPENAI = "openai"

#: OpenAI ``finish_reason`` -> Anthropic ``stop_reason``.
STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}

#: HTTP status -> Anthropic error ``type``.
ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    529: "overloaded_error",
}

#: ``thinking.budget_tokens`` upper bounds -> the effort word it maps to.
_BUDGET_EFFORT = ((4096, "low"), (16384, "medium"))


class TranslationError(ValueError):
    """A Messages request this module cannot express as Chat Completions."""


# --------------------------------------------------------------------------- #
# request
# --------------------------------------------------------------------------- #
def _text_of(content: Any) -> str:
    """Plain text of a string or a list of text blocks."""
    if isinstance(content, str):
        return content
    parts: List[str] = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n\n".join(p for p in parts if p)


def _image_part(block: dict) -> Optional[dict]:
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    if source.get("type") == "base64":
        media = source.get("media_type") or "image/png"
        url = f"data:{media};base64,{source.get('data') or ''}"
    elif source.get("type") == "url":
        url = str(source.get("url") or "")
    else:
        return None
    return {"type": "image_url", "image_url": {"url": url}}


def _document_text(block: dict) -> str:
    source = block.get("source")
    if isinstance(source, dict) and source.get("type") == "text":
        return str(source.get("data") or "")
    title = block.get("title") or "document"
    return f"[{title}: not carried by the OpenAI translation]"


def _user_parts(blocks: Iterable[dict]) -> List[dict]:
    """Chat content parts for the non-tool blocks of a user turn."""
    parts: List[dict] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            parts.append({"type": "text", "text": str(block.get("text") or "")})
        elif kind == "image":
            part = _image_part(block)
            if part is not None:
                parts.append(part)
        elif kind == "document":
            parts.append({"type": "text", "text": _document_text(block)})
    return parts


def _collapse(parts: List[dict]):
    """A plain string when every part is text, else the part list."""
    if all(p.get("type") == "text" for p in parts):
        return "\n\n".join(p["text"] for p in parts)
    return parts


def _tool_result_message(block: dict) -> tuple:
    """``(tool message, image parts to carry in a user message)``."""
    content = block.get("content")
    images: List[dict] = []
    if isinstance(content, list):
        texts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
            elif item.get("type") == "image":
                part = _image_part(item)
                if part is not None:
                    images.append(part)
        text = "\n\n".join(texts)
    else:
        text = "" if content is None else str(content)
    if block.get("is_error"):
        text = f"Error: {text}" if text else "Error"
    message = {
        "role": "tool",
        "tool_call_id": str(block.get("tool_use_id") or ""),
        "content": text,
    }
    return message, images


def _user_messages(content: Any) -> List[dict]:
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    out: List[dict] = []
    carried: List[dict] = []
    # Tool results answer the assistant's tool calls, so they go first and
    # each as its own ``tool`` message; whatever else the turn says follows.
    for block in blocks:
        if block.get("type") == "tool_result":
            message, images = _tool_result_message(block)
            out.append(message)
            carried.extend(images)
    parts = _user_parts(b for b in blocks if b.get("type") != "tool_result")
    if carried:
        parts = [{"type": "text", "text": "(images from the tool results above)"}] \
            + carried + parts
    if parts:
        out.append({"role": "user", "content": _collapse(parts)})
    return out


def _assistant_message(content: Any, reasoning_format: Optional[str]) -> Optional[dict]:
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    texts: List[str] = []
    thinking: List[str] = []
    calls: List[dict] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            texts.append(str(block.get("text") or ""))
        elif kind == "thinking":
            thinking.append(str(block.get("thinking") or ""))
        elif kind == "tool_use":
            calls.append({
                "id": str(block.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(block.get("name") or ""),
                    "arguments": json.dumps(block.get("input") or {},
                                            ensure_ascii=False),
                },
            })
    if not (texts or calls or thinking):
        return None
    message: Dict[str, Any] = {"role": "assistant",
                               "content": "".join(texts) if texts else None}
    if calls:
        message["tool_calls"] = calls
    # DeepSeek's thinking mode wants its reasoning back on the turns that made
    # tool calls; the OpenAI encoding has no field for it.
    if thinking and reasoning_format == "deepseek":
        message["reasoning_content"] = "".join(thinking)
    return message


def _tools(tools: Any) -> List[dict]:
    out = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict) or "input_schema" not in tool:
            continue  # a server tool (web search...): no OpenAI equivalent
        fn: Dict[str, Any] = {"name": str(tool.get("name") or ""),
                              "parameters": tool.get("input_schema") or {}}
        if tool.get("description"):
            fn["description"] = str(tool["description"])
        out.append({"type": "function", "function": fn})
    return out


def _tool_choice(choice: Any):
    """``(tool_choice, parallel_tool_calls)`` for an Anthropic ``tool_choice``."""
    if not isinstance(choice, dict):
        return None, None
    kind = choice.get("type")
    parallel = False if choice.get("disable_parallel_tool_use") else None
    if kind == "auto":
        return "auto", parallel
    if kind == "any":
        return "required", parallel
    if kind == "none":
        return "none", None
    if kind == "tool":
        return {"type": "function", "function": {"name": str(choice.get("name") or "")}}, parallel
    return None, None


def _effort(body: dict, configured: Optional[str]) -> Optional[str]:
    if configured:
        return configured
    output = body.get("output_config")
    if isinstance(output, dict) and output.get("effort"):
        return str(output["effort"])
    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        budget = thinking.get("budget_tokens")
        if isinstance(budget, int):
            for bound, word in _BUDGET_EFFORT:
                if budget < bound:
                    return word
            return "high"
    return None


def _apply_thinking(out: dict, body: dict, reasoning_format: Optional[str],
                    effort: Optional[str]) -> None:
    thinking = body.get("thinking")
    kind = thinking.get("type") if isinstance(thinking, dict) else None
    if kind == "disabled":
        if reasoning_format == "deepseek":
            out["thinking"] = {"type": "disabled"}
        return
    if kind not in ("enabled", "adaptive"):
        return
    word = _effort(body, effort)
    if reasoning_format == "deepseek":
        out["thinking"] = {"type": "enabled"}
    if word and reasoning_format in ("deepseek", "openai"):
        out["reasoning_effort"] = word


def request(body: dict, *, reasoning_format: Optional[str] = None,
            reasoning_effort: Optional[str] = None) -> dict:
    """The Chat Completions body for an Anthropic Messages ``body``.

    ``reasoning_format`` is the provider's ``openai_reasoning_format``
    (``deepseek`` / ``openai`` / ``None``) and decides how ``thinking`` is
    carried; ``reasoning_effort`` is the provider's configured effort, which
    wins over one derived from the request.
    """
    if not isinstance(body, dict):
        raise TranslationError("a Messages request body must be a JSON object")
    messages: List[dict] = []
    system = _text_of(body.get("system")) if body.get("system") is not None else ""
    if system:
        messages.append({"role": "system", "content": system})
    for turn in body.get("messages") or []:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        if role == "user":
            messages.extend(_user_messages(turn.get("content")))
        elif role == "assistant":
            message = _assistant_message(turn.get("content"), reasoning_format)
            if message is not None:
                messages.append(message)
    out: Dict[str, Any] = {"model": body.get("model"), "messages": messages}
    if body.get("max_tokens") is not None:
        out["max_tokens"] = body["max_tokens"]
    for key in ("temperature", "top_p"):
        if body.get(key) is not None:
            out[key] = body[key]
    if body.get("stop_sequences"):
        out["stop"] = list(body["stop_sequences"])
    tools = _tools(body.get("tools"))
    if tools:
        out["tools"] = tools
        choice, parallel = _tool_choice(body.get("tool_choice"))
        if choice is not None:
            out["tool_choice"] = choice
        if parallel is not None:
            out["parallel_tool_calls"] = parallel
    _apply_thinking(out, body, reasoning_format, reasoning_effort)
    if body.get("stream"):
        out["stream"] = True
        out["stream_options"] = {"include_usage": True}
    return out


def count_tokens(body: dict) -> dict:
    """A local estimate for ``/v1/messages/count_tokens``.

    Chat Completions has no counting endpoint. Four characters per token is
    the usual rough rule; Claude Code uses the count to decide when to
    compact, so an estimate is better than a failure.
    """
    text = json.dumps(
        {k: body.get(k) for k in ("system", "messages", "tools")},
        ensure_ascii=False,
    ) if isinstance(body, dict) else ""
    return {"input_tokens": max(1, len(text) // 4)}


# --------------------------------------------------------------------------- #
# response
# --------------------------------------------------------------------------- #
def usage(raw: Any) -> dict:
    """Anthropic usage for an OpenAI ``usage`` object.

    OpenAI's ``prompt_tokens`` includes the cached part
    (``prompt_tokens_details.cached_tokens``; DeepSeek reports it as
    ``prompt_cache_hit_tokens``); Anthropic's ``input_tokens`` does not.
    """
    raw = raw if isinstance(raw, dict) else {}
    prompt = int(raw.get("prompt_tokens") or 0)
    details = raw.get("prompt_tokens_details")
    cached = int(
        (details.get("cached_tokens") if isinstance(details, dict) else 0)
        or raw.get("prompt_cache_hit_tokens")
        or 0
    )
    return {
        "input_tokens": max(0, prompt - cached),
        "output_tokens": int(raw.get("completion_tokens") or 0),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
    }


def _reasoning_of(message: dict) -> str:
    value = message.get("reasoning_content")
    if value is None:
        value = message.get("reasoning")
    return value if isinstance(value, str) else ""


def _parse_arguments(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"_raw_arguments": raw}
    return value if isinstance(value, dict) else {"_value": value}


def message_id(raw: Any = None) -> str:
    text = str(raw or "").strip()
    return text if text.startswith("msg_") else f"msg_{text or uuid.uuid4().hex}"


def response(doc: dict, *, model: Optional[str] = None) -> dict:
    """The Messages response for a non-streamed Chat Completions ``doc``."""
    choices = doc.get("choices") if isinstance(doc, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    message = message if isinstance(message, dict) else {}
    content: List[dict] = []
    reasoning = _reasoning_of(message)
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    text = message.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        content.append({
            "type": "tool_use",
            "id": str(call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}"),
            "name": str(fn.get("name") or ""),
            "input": _parse_arguments(fn.get("arguments")),
        })
    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    stop = STOP_REASONS.get(str(finish), "end_turn") if finish else "end_turn"
    if stop == "end_turn" and any(b["type"] == "tool_use" for b in content):
        stop = "tool_use"
    return {
        "id": message_id(doc.get("id")),
        "type": "message",
        "role": "assistant",
        "model": doc.get("model") or model or "",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage(doc.get("usage")),
    }


def error(status: int, raw: bytes) -> dict:
    """An Anthropic error body for an upstream failure ``raw`` at ``status``."""
    message = ""
    try:
        doc = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        err = doc.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or "")
        elif isinstance(err, str):
            message = err
        message = message or str(doc.get("message") or "")
    if not message:
        message = raw.decode("utf-8", "replace").strip()[:2000] or f"HTTP {status}"
    kind = ERROR_TYPES.get(status, "api_error" if status >= 500 else "invalid_request_error")
    return {"type": "error", "error": {"type": kind, "message": message}}


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


class StreamTranslator:
    """OpenAI ``chat.completion.chunk`` SSE bytes in, Anthropic SSE bytes out.

    Feed the upstream bytes as they arrive (:meth:`feed`); each call returns
    the Anthropic events those bytes complete. :meth:`close` ends the message
    (``message_delta`` + ``message_stop``) — call it once the upstream stream
    is over, whether or not it sent ``[DONE]``.

    An upstream error chunk becomes an ``error`` event and ends the stream:
    Anthropic clients treat ``error`` as terminal, so nothing follows it, not
    even the closing events.

    Tool calls: the first call streams live. Anthropic content blocks cannot
    reopen, while OpenAI may interleave argument fragments across calls, so
    once that first call is open every later block (other calls, text,
    thinking) is held and emitted whole when the stream ends. Fragments for
    the live call keep streaming; nothing is dropped.
    """

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or ""
        self._buffer = b""
        self._started = False
        self._closed = False
        self._index = -1
        self._open: Optional[str] = None  # "text" | "thinking" | "tool:<n>"
        self._tools: Dict[int, int] = {}
        self._finish: Optional[str] = None
        self._usage: dict = {}
        self._id: Optional[str] = None
        self._saw_tool = False
        self._errored = False
        self._live: Optional[int] = None  # position of the call streaming live
        self._pending: List[dict] = []  # held blocks, in the order they began
        self._held_tools: Dict[int, dict] = {}

    # -- events --------------------------------------------------------------
    def _start(self, out: List[bytes]) -> None:
        if self._started:
            return
        self._started = True
        out.append(sse("message_start", {
            "type": "message_start",
            "message": {
                "id": message_id(self._id),
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }))

    def _stop_block(self, out: List[bytes]) -> None:
        if self._open is not None:
            out.append(sse("content_block_stop",
                           {"type": "content_block_stop", "index": self._index}))
            self._open = None

    def _open_block(self, out: List[bytes], kind: str, block: dict) -> None:
        self._stop_block(out)
        self._index += 1
        self._open = kind
        out.append(sse("content_block_start", {
            "type": "content_block_start", "index": self._index,
            "content_block": block,
        }))

    def _delta(self, out: List[bytes], delta: dict) -> None:
        out.append(sse("content_block_delta", {
            "type": "content_block_delta", "index": self._index, "delta": delta,
        }))

    # -- input ---------------------------------------------------------------
    def feed(self, data: bytes) -> List[bytes]:
        out: List[bytes] = []
        self._buffer += data
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._line(line.strip(), out)
        return out

    def _line(self, line: bytes, out: List[bytes]) -> None:
        if self._errored or not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            doc = json.loads(payload.decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(doc, dict):
            return
        if isinstance(doc.get("error"), dict):
            self._start(out)
            self._stop_block(out)
            err = doc["error"]
            out.append(sse("error", {"type": "error", "error": {
                "type": "api_error", "message": str(err.get("message") or err)}}))
            self._errored = True
            return
        self._id = self._id or doc.get("id")
        if doc.get("model") and not self.model:
            self.model = str(doc["model"])
        if isinstance(doc.get("usage"), dict):
            self._usage = doc["usage"]
        for choice in doc.get("choices") or []:
            if isinstance(choice, dict):
                self._choice(choice, out)

    def _choice(self, choice: dict, out: List[bytes]) -> None:
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        reasoning = _reasoning_of(delta)
        if reasoning:
            self._start(out)
            if self._live is not None:
                self._hold_text("thinking", reasoning)
            else:
                if self._open != "thinking":
                    self._open_block(out, "thinking",
                                     {"type": "thinking", "thinking": "", "signature": ""})
                self._delta(out, {"type": "thinking_delta", "thinking": reasoning})
        text = delta.get("content")
        if isinstance(text, str) and text:
            self._start(out)
            if self._live is not None:
                self._hold_text("text", text)
            else:
                if self._open != "text":
                    self._open_block(out, "text", {"type": "text", "text": ""})
                self._delta(out, {"type": "text_delta", "text": text})
        for call in delta.get("tool_calls") or []:
            if isinstance(call, dict):
                self._tool_call(call, out)
        if choice.get("finish_reason"):
            self._finish = str(choice["finish_reason"])

    def _hold_text(self, kind: str, text: str) -> None:
        last = self._pending[-1] if self._pending else None
        if last is not None and last["kind"] == kind:
            last["text"] += text
        else:
            self._pending.append({"kind": kind, "text": text})

    def _tool_call(self, call: dict, out: List[bytes]) -> None:
        self._start(out)
        position = call.get("index")
        position = position if isinstance(position, int) else len(self._tools)
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        arguments = fn.get("arguments")
        arguments = arguments if isinstance(arguments, str) else ""
        if position not in self._tools:
            self._saw_tool = True
            self._tools[position] = position
            block = {
                "type": "tool_use",
                "id": str(call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}"),
                "name": str(fn.get("name") or ""),
                "input": {},
            }
            if self._live is None:
                self._live = position
                self._open_block(out, f"tool:{position}", block)
            else:
                held = {"kind": "tool", "block": block, "args": ""}
                self._held_tools[position] = held
                self._pending.append(held)
        if position == self._live:
            if arguments:
                self._delta(out, {"type": "input_json_delta", "partial_json": arguments})
            return
        held = self._held_tools[position]
        if fn.get("name") and not held["block"]["name"]:
            held["block"]["name"] = str(fn["name"])
        held["args"] += arguments

    def _flush_pending(self, out: List[bytes]) -> None:
        for held in self._pending:
            if held["kind"] == "tool":
                self._open_block(out, "tool", held["block"])
                if held["args"]:
                    self._delta(out, {"type": "input_json_delta",
                                      "partial_json": held["args"]})
            elif held["kind"] == "thinking":
                self._open_block(out, "thinking",
                                 {"type": "thinking", "thinking": "", "signature": ""})
                self._delta(out, {"type": "thinking_delta", "thinking": held["text"]})
            else:
                self._open_block(out, "text", {"type": "text", "text": ""})
                self._delta(out, {"type": "text_delta", "text": held["text"]})
        self._pending = []
        self._stop_block(out)

    def close(self) -> List[bytes]:
        out: List[bytes] = []
        if self._closed:
            return out
        self._closed = True
        if self._buffer.strip():
            self._line(self._buffer.strip(), out)
            self._buffer = b""
        if self._errored:
            return out
        self._start(out)
        self._stop_block(out)
        self._flush_pending(out)
        stop = STOP_REASONS.get(self._finish or "", "end_turn")
        if stop == "end_turn" and self._saw_tool:
            stop = "tool_use"
        usage_doc = usage(self._usage)
        out.append(sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": usage_doc,
        }))
        out.append(sse("message_stop", {"type": "message_stop"}))
        return out
