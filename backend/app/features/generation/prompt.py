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
5. A request to summarise, list, or explain the key points of a topic is answered by
   combining every passage that states one of those points — one point per sentence, each
   cited to the passage it came from. This is not the same as no passage answering the
   question: give the abstention sentence only when the passages do not address the topic
   at all, never merely because the full answer takes more than one passage to state.
6. Answer the exact question asked. If it asks for two things, answer both.
7. Start with the answer itself. Do not write "According to passage 2" or "The passages
   say" — state the fact and put the marker after it.
8. Write the answer yourself. Do not copy sentences out of a passage, and never write "we"
   or "our" — the passages were written by their authors, not by you.
9. [1] is a citation marker, not a name. When you name a document, write its filename and
   nothing else — not the marker, not the page number.
10. Be brief, but never answer with only "yes" or "no". State the fact that makes it so."""


def build(question: str, hits: list[Hit]) -> str:
    """Number the passages from 1, and keep that numbering as the only handle the model has.

    Chunk ids are never shown. A UUID in the prompt is 36 tokens of nothing the model can
    reason about, and it invites the model to invent one — an invented `[3]` is caught by
    range-checking, an invented UUID would have to be checked against the database, which
    means a lookup, which means the model could name a passage it was never shown.
    """
    passages = "\n\n".join(
        # Delimited, and with the filename before the text rather than in a parenthesis
        # after the marker. Measured: an 8B model answering "which document states the
        # withholding rates" replied "the document is [3] (from irs-pub-15, page 15)" —
        # it read the marker as the document's name because the marker came first and the
        # filename looked like an aside. The triple quotes matter for the same reason
        # rule 7 exists: without a visible boundary the model treats passage prose as its
        # own voice and copies it out, first person and all.
        f'[{number}] {hit.filename} — page {hit.page_num}\n"""\n{hit.text}\n"""'
        for number, hit in enumerate(hits, start=1)
    )
    return f"Passages:\n\n{passages}\n\nQuestion: {question}\n\nAnswer:"
