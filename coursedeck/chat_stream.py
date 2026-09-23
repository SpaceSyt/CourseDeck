"""Visible answer deltas only; model reasoning fields are never forwarded."""

import json

OUTPUT_TOKEN_LIMIT = 8192


class CompletionLengthExceeded(ValueError):
    pass


def buffered_message(result):
    choice = result["choices"][0]
    if choice.get("finish_reason") == "length":
        raise CompletionLengthExceeded("Provider reply reached its output limit")
    if choice.get("finish_reason") not in {None, "stop", "tool_calls"}:
        raise ValueError("Provider reply was incomplete")
    message = choice["message"]
    if not isinstance(message, dict):
        raise ValueError("Invalid model response")
    content = message.get("content") or ""
    if not isinstance(content, str) or len(content) > 64000:
        raise ValueError("Invalid or oversized model reply")
    return message


class VisibleText:
    def __init__(self):
        self.pending = ""
        self.hidden = None

    def feed(self, text, final=False):
        self.pending += text
        visible = ""
        tags = ["think", "analysis", "reasoning"]
        while self.pending:
            candidates = [f"</{self.hidden}>"] if self.hidden else [f"<{tag}>" for tag in tags]
            found = [(self.pending.find(tag), tag) for tag in candidates if tag in self.pending]
            if found:
                at, tag = min(found)
                if not self.hidden:
                    visible += self.pending[:at]
                    self.hidden = tag[1:-1]
                else:
                    self.hidden = None
                self.pending = self.pending[at + len(tag) :]
                continue
            keep = 0
            if not final:
                for tag in candidates:
                    for count in range(1, min(len(tag), len(self.pending) + 1)):
                        if self.pending.endswith(tag[:count]):
                            keep = max(keep, count)
            cut = len(self.pending) - keep
            if not self.hidden:
                visible += self.pending[:cut]
            self.pending = self.pending[cut:]
            break
        return visible


async def stream_completion(client, config, key, messages, tools, emit):
    payload = {
        "model": config["model"],
        "messages": messages,
        "max_tokens": OUTPUT_TOKEN_LIMIT,
        "stream": True,
    }
    if tools:
        payload.update(tools=tools, tool_choice="auto")
    headers = {"Authorization": "Bearer " + key} if key else {}
    for attempt in range(2):
        async with client.stream(
            "POST", config["base_url"] + "/chat/completions", json=payload, headers=headers
        ) as response:
            if response.status_code == 400 and not attempt:
                await response.aread()
                try:
                    param = response.json().get("error", {}).get("param")
                except (ValueError, AttributeError):
                    param = None
                if param == "max_tokens":
                    payload["max_completion_tokens"] = payload.pop("max_tokens")
                    continue
            response.raise_for_status()
            await emit({"type": "answer_start"})
            if "application/json" in response.headers.get("content-type", ""):
                await response.aread()
                message = buffered_message(response.json())
                content = VisibleText().feed(message.get("content") or "", final=True)
                await emit({"type": "activity", "label": "Provider returned a buffered reply"})
                if content:
                    await emit({"type": "delta", "text": content})
                return {"content": content, "tool_calls": message.get("tool_calls", [])}
            content, calls, finished = "", {}, False
            visible = VisibleText()
            async for line in response.aiter_lines():
                if len(line) > 150_000:
                    raise ValueError("Stream event exceeds limit")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("error"):
                    raise ValueError("Provider stream failed")
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                # reasoning_content/reasoning/details are deliberately not read.
                text = visible.feed(delta.get("content") or "")
                if text:
                    content += text
                    if len(content) > 64000:
                        raise ValueError("Answer exceeds limit")
                    await emit({"type": "delta", "text": text})
                for part in delta.get("tool_calls") or []:
                    index = part["index"]
                    if type(index) is not int or not 0 <= index < 4:
                        raise ValueError("Invalid tool index")
                    call = calls.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if part.get("id"):
                        call["id"] += part["id"]
                    for field in ("name", "arguments"):
                        call["function"][field] += (part.get("function") or {}).get(field) or ""
                        if len(call["function"][field]) > 32000:
                            raise ValueError("Tool arguments exceed limit")
                if choice.get("finish_reason"):
                    if choice["finish_reason"] == "length":
                        raise CompletionLengthExceeded("Provider reply reached its output limit")
                    if choice["finish_reason"] not in {"stop", "tool_calls"}:
                        raise ValueError("Provider reply was incomplete")
                    finished = True
            if not finished:
                raise ValueError("Provider stream ended before completion")
            tail = visible.feed("", final=True)
            if tail:
                content += tail
                await emit({"type": "delta", "text": tail})
            result = sorted(calls.items())
            if any(not call["id"] or not call["function"]["name"] for _, call in result):
                raise ValueError("Incomplete tool call")
            return {"content": content, "tool_calls": [call for _, call in result]}
