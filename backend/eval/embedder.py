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
    """PyTorch with the MPS backend. For the development machine."""

    def encode(self, texts: list[str], batch: int = 16) -> list[list[float]]:
        import torch
        from sentence_transformers import SentenceTransformer

        device = "mps" if torch.backends.mps.is_available() else "cpu"
        model = SentenceTransformer(MODEL, device=device)
        vectors = model.encode(
            texts, batch_size=batch, normalize_embeddings=True, show_progress_bar=True
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
