"""The instructions, written for the worst model a customer will plug in.

mvp.md 5.4 fixed the development model at Llama 3.1 8B Instruct for exactly this reason:
prompts tuned against a large cloud model produce instructions an 8B breaks, and the
breakage appears at the first customer deploying locally. Everything here is phrased for
a model that follows simple rules and ignores subtle ones.

None of it is a guarantee. The prompt asks for citations; `citations.py` decides whether
what came back has any. This file is the request, not the enforcement.
"""

from app.features.retrieval.search import Hit

# The exact sentence, so `citations.py` can recognise it and so the UI can render an
# abstention differently from an answer. Matching on a phrase the model invented would be
# guesswork; matching on one it was handed is a contract.
ABSTENTION = "The documents provided do not contain an answer to this question."

SYSTEM = f"""You answer questions using only the numbered passages provided.

Rules:
1. Use only what the passages say. Never use your own knowledge of the subject.
2. After every sentence containing a fact, cite the passage it came from, like this: [1].
   If a sentence uses two passages, write [1][2].
3. Only cite numbers that appear in the passages given to you.
4. If the passages do not answer the question, reply with exactly this sentence and nothing
   else: {ABSTENTION}
5. Be brief. Answer the question that was asked, and stop."""


def build(question: str, hits: list[Hit]) -> str:
    """Number the passages from 1, and keep that numbering as the only handle the model has.

    Chunk ids are never shown. A UUID in the prompt is 36 tokens of nothing the model can
    reason about, and it invites the model to invent one — an invented `[3]` is caught by
    range-checking, an invented UUID would have to be checked against the database, which
    means a lookup, which means the model could name a passage it was never shown.
    """
    passages = "\n\n".join(
        f"[{number}] (from {hit.filename}, page {hit.page_num})\n{hit.text}"
        for number, hit in enumerate(hits, start=1)
    )
    return f"Passages:\n\n{passages}\n\nQuestion: {question}"
