# A scripted chat client. Returns what it is told to return.
#
# This is what makes the verification tests possible at all. To prove that
# numeric_fidelity fails a tampered numeral you need a model that emits one on
# demand, and no real model does that reliably - you would be testing the
# model's mood rather than your own guard. A scripted responder turns "does
# the guard catch it" into a deterministic question.

from collections.abc import AsyncIterator


class ScriptedChatClient:
    """Satisfies medw_core.ports.ChatClient.

    `responses` is consumed in order; when exhausted it repeats the last one so
    a test that makes an unexpected extra call fails on the assertion rather
    than on an IndexError three frames away.
    """

    def __init__(self, responses: list[str] | None = None,
                 json_responses: list[dict] | None = None):
        self.responses = responses or ["connective prose only."]
        self.json_responses = json_responses or [{}]
        self.calls: list[str] = []          # every prompt seen, for assertions

    def _next(self, seq: list):
        return seq[min(len(self.calls) - 1, len(seq) - 1)]

    async def stream(self, prompt: str, *, max_tokens: int = 2048) -> AsyncIterator[str]:
        self.calls.append(prompt)
        # Yields token-by-token so a consumer that assumes streaming is
        # exercised as streaming, not handed one lump.
        for word in self._next(self.responses).split():
            yield word + " "

    async def complete_json(self, prompt: str, schema: dict) -> dict:
        self.calls.append(prompt)
        return self._next(self.json_responses)
