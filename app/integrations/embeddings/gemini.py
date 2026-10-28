import math
from collections.abc import Sequence

import httpx


class GeminiEmbeddingProvider:
    """Gemini text embedding adapter; use document/query task types asymmetrically."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-embedding-001",
        dimensions: int = 768,
        timeout_seconds: int = 20,
    ) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for Gemini embeddings.")
        if dimensions not in {128, 256, 512, 768, 1536, 2048, 3072}:
            raise ValueError("Unsupported Gemini embedding dimension.")
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._timeout_seconds = timeout_seconds

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_document(self, text: str, title: str | None = None) -> list[float]:
        return await self._embed(text, task_type="RETRIEVAL_DOCUMENT", title=title)

    async def embed_documents(
        self,
        texts: Sequence[str],
        *,
        title: str | None = None,
        batch_size: int = 32,
    ) -> list[list[float]]:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive.")
        if not texts:
            return []
        for text in texts:
            if not text.strip():
                raise ValueError("Cannot embed empty text.")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self._model}:batchEmbedContents"
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            for start in range(0, len(texts), batch_size):
                batch = texts[start : start + batch_size]
                requests_payload: list[dict[str, object]] = []
                for text in batch:
                    req: dict[str, object] = {
                        "model": f"models/{self._model}",
                        "content": {"parts": [{"text": text}]},
                        "taskType": "RETRIEVAL_DOCUMENT",
                        "outputDimensionality": self._dimensions,
                    }
                    if title:
                        req["title"] = title
                    requests_payload.append(req)

                response = await client.post(
                    url,
                    headers={"x-goog-api-key": self._api_key},
                    json={"requests": requests_payload},
                )
                response.raise_for_status()
                embeddings = response.json().get("embeddings", [])
                if len(embeddings) != len(batch):
                    raise ValueError("Embedding provider returned an unexpected batch count.")
                for item in embeddings:
                    values = item.get("values", [])
                    if len(values) != self._dimensions:
                        raise ValueError(
                            "Embedding provider returned an unexpected vector dimension."
                        )
                    vectors.append(_normalize([float(value) for value in values]))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return await self._embed(text, task_type="RETRIEVAL_QUERY")

    async def _embed(self, text: str, *, task_type: str, title: str | None = None) -> list[float]:
        if not text.strip():
            raise ValueError("Cannot embed empty text.")
        payload: dict[str, object] = {
            "content": {"parts": [{"text": text}]},
            "taskType": task_type,
            "outputDimensionality": self._dimensions,
        }
        if title and task_type == "RETRIEVAL_DOCUMENT":
            payload["title"] = title
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self._model}:embedContent"
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                url,
                headers={"x-goog-api-key": self._api_key},
                json=payload,
            )
        response.raise_for_status()
        values = response.json()["embedding"]["values"]
        if len(values) != self._dimensions:
            raise ValueError("Embedding provider returned an unexpected vector dimension.")
        return _normalize([float(value) for value in values])


def _normalize(values: list[float]) -> list[float]:
    magnitude = math.sqrt(sum(value * value for value in values))
    if magnitude == 0:
        raise ValueError("Embedding provider returned a zero vector.")
    return [value / magnitude for value in values]
