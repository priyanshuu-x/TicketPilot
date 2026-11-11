from collections.abc import Sequence
from functools import cache

from fastembed import TextEmbedding

EMBED_DIMS = 384


@cache
def _model() -> TextEmbedding:
    return TextEmbedding("BAAI/bge-small-en-v1.5")


def embed(texts: Sequence[str]) -> list[list[float]]:
    return [vector.tolist() for vector in _model().embed(list(texts))]
