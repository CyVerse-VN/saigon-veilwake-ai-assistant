from collections.abc import Iterable


def reciprocal_rank_fusion(
    ranked_lists: Iterable[Iterable[str]], *, k: int = 60
) -> list[tuple[str, float]]:
    """Combine lexical and semantic ranks without comparing incompatible score scales."""

    scores: dict[str, float] = {}
    for ranked_list in ranked_lists:
        for position, item_id in enumerate(ranked_list, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + position)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)
