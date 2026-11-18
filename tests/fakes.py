"""A scripted stand-in for AsyncAnthropic so tests run without network or an API key."""

from __future__ import annotations

from types import SimpleNamespace


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, input: dict, id: str = "toolu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=input, id=id)


def message(*blocks, stop_reason: str = "end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


class _Stream:
    def __init__(self, msg):
        self._msg = msg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for b in self._msg.content:
                if b.type == "text":
                    yield SimpleNamespace(type="text", text=b.text)
                elif b.type == "tool_use":
                    yield SimpleNamespace(type="content_block_start", content_block=b)
        return gen()

    async def get_final_message(self):
        return self._msg


class FakeClient:
    """Each call to beta.messages.stream() pops the next scripted message."""

    def __init__(self, script: list):
        self.script = list(script)
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params):
        # Snapshot messages: the agent keeps appending to the same list.
        self.requests.append({**params, "messages": list(params["messages"])})
        if not self.script:
            raise AssertionError("FakeClient ran out of scripted responses")
        return _Stream(self.script.pop(0))
