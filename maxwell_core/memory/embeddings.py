"""Embedding response parsing and defensive vector normalization."""

import numpy as np


def extract_embeddings(data: dict) -> list[list[float]]:
    """Read Ollama/OpenAI responses, honoring OpenAI's batch input indices."""
    if not isinstance(data, dict):
        return []
    for key in ("embeddings", "embedding"):
        vectors = data.get(key)
        if isinstance(vectors, list) and vectors:
            return vectors if key == "embeddings" else [vectors]
    items = data.get("data")
    if not isinstance(items, list) or not items:
        return []
    if any(
        not isinstance(item, dict) or not isinstance(item.get("embedding"), list)
        for item in items
    ):
        return []
    if any("index" in item for item in items):
        indices = [item.get("index") for item in items]
        if any(type(index) is not int for index in indices):
            return []
        if sorted(indices) != list(range(len(items))):
            return []
        items = sorted(items, key=lambda item: item["index"])
    return [item["embedding"] for item in items]


def normalized_vector(vector, dimension: int) -> np.ndarray | None:
    """Copy a finite nonzero vector so caches/callers never change in place."""
    try:
        value = np.array(vector, dtype=np.float32, copy=True)
        if value.shape != (dimension,) or not np.isfinite(value).all():
            return None
        norm = float(np.linalg.norm(value))
        if not np.isfinite(norm) or norm <= 1e-8:
            return None
        return value / norm
    except (TypeError, ValueError, OverflowError):
        return None
