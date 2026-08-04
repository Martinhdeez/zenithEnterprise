# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportMissingTypeStubs=false, reportUnknownArgumentType=false
#
# sentence-transformers ships partial stubs. Suppressed once for this file, as in
# the other laboratory modules; nothing under `app/` relaxes strictness.

"""Two ways to reach the same weights.

BGE-M3 is served by `text-embeddings-inference` in production, and TEI publishes
`linux/amd64` only. On Apple Silicon that means emulation, so the development machine runs
the model through PyTorch instead. **The weights are identical, so the same text produces
the same vector and the same ranking** — which is what lets quality measurements run on a
laptop and resource measurements run on the machine a customer would actually use.

That split is not a convenience. Timing an emulated container would produce numbers worse
than the cheap VPS and wrong in a way no correction factor repairs.
"""

import os
from typing import Protocol

MODEL = "BAAI/bge-m3"
DIMENSION = 1024


class Embedder(Protocol):
    def encode(self, texts: list[str], batch: int) -> list[list[float]]: ...


class LocalEmbedder:
    """PyTorch with the MPS backend. For the development machine.

    The model is loaded once per instance rather than once per call, and that is not a
    micro-optimisation. Ingestion calls `encode` once with every chunk, so the difference is
    invisible there — but `LocalQueryEmbedder.embed_query` calls it **once per question**,
    and the original version therefore reloaded two gigabytes of weights before every
    query. It is why every per-query latency this harness has ever reported was dominated
    by model loading, and why F6's 4.8 s and F7's 8.6 s medians were correctly described in
    both write-ups as meaning nothing.
    """

    def __init__(self) -> None:
        self._model: object | None = None

    def _loaded(self) -> object:
        import torch
        from sentence_transformers import SentenceTransformer

        if self._model is None:
            device = "mps" if torch.backends.mps.is_available() else "cpu"
            self._model = SentenceTransformer(MODEL, device=device)
        return self._model

    def encode(self, texts: list[str], batch: int = 16) -> list[list[float]]:
        vectors = self._loaded().encode(  # type: ignore[attr-defined]
            texts,
            batch_size=batch,
            normalize_embeddings=True,
            # Off for a single query: a progress bar per question buries the actual output
            # of a 36-question run under 36 finished bars.
            show_progress_bar=len(texts) > 1,
        )
        return [vector.tolist() for vector in vectors]


class TeiEmbedder:
    """The production serving path, over HTTP.

    Batches are kept small because this runs on a machine with roughly four gigabytes free:
    TEI holds the model plus the activations for a whole batch, and the batch size is what
    decides peak memory. Sending everything at once is how the measurement becomes an OOM
    kill instead of a number.
    """

    def __init__(self, url: str | None = None) -> None:
        self.url = (url or os.environ.get("ZENITH_TEI_EMBED_URL", "http://localhost:8081")).rstrip(
            "/"
        )

    def encode(self, texts: list[str], batch: int = 4) -> list[list[float]]:
        """The batch must fit TEI's `--max-batch-tokens`, not merely its client limit.

        Learned by 413. On the low-spec profile TEI runs with `--max-batch-tokens 2048`,
        and a 1,200-character chunk is roughly 350 tokens, so eight of them is 2,800 and
        every request fails. The two numbers are one setting in two places, which is
        exactly the kind of pair a hardware profile exists to keep together.
        """
        import httpx

        vectors: list[list[float]] = []
        with httpx.Client(timeout=600.0) as client:
            for start in range(0, len(texts), batch):
                window = texts[start : start + batch]
                response = client.post(f"{self.url}/embed", json={"inputs": window})
                response.raise_for_status()
                vectors.extend(response.json())
                if start and start % (batch * 25) == 0:
                    print(f"  embedded {start}/{len(texts)}", flush=True)
        return vectors


def get_embedder() -> Embedder:
    """TEI when its URL is configured, PyTorch otherwise.

    Configuration rather than platform detection: the VPS run sets the variable, and any
    later machine that wants the production path gets it by pointing at a TEI instance
    rather than by being a particular architecture.
    """
    if os.environ.get("ZENITH_TEI_EMBED_URL"):
        return TeiEmbedder()
    return LocalEmbedder()
