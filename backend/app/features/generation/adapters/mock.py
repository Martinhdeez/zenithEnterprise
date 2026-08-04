"""A provider that answers from a script, and ships rather than living in a test file.

Three jobs, and the third is the reason it is not test-only code:

1. **Tests** get a provider with no network and no model, so a citation-binding assertion
   fails for one reason only.
2. **`zenith diagnose`** and a customer's first run get an installation that answers
   without an LLM configured, which is what makes "is retrieval working?" separable from
   "is the model reachable?" when both are new.
3. **The interface gets a second implementation.** An abstraction with one implementation
   is a guess; the compiler cannot tell you whether `BaseLLMProvider` is actually general
   until something other than an HTTP client satisfies it.

It answers by citing the first passage, because the alternative — answering with no
citation — would be discarded by the binder and turn every mocked run into an abstention.
"""

from app.common.llm import BaseLLMProvider, GenerationResponse

MODEL = "zenith-mock"


class MockProvider(BaseLLMProvider):
    name = "mock"

    def __init__(self, script: list[str] | None = None, model: str = MODEL) -> None:
        """`script` is answered in order, then repeated from the last entry.

        Repeating rather than raising: a test that asserts on the first answer should not
        also have to know how many times the service calls the model, and a diagnose run
        answers an unbounded number of questions.
        """
        self.script = list(script) if script else ["This is a mock answer [1]."]
        self.model = model
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str) -> GenerationResponse:
        self.calls.append((system, user))
        index = min(len(self.calls) - 1, len(self.script) - 1)
        return GenerationResponse(text=self.script[index], model=self.model)
