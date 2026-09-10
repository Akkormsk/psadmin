"""Обратный индекс с точной семантикой catalog._stem_in_words."""

from collections import Counter, defaultdict
from functools import lru_cache


@lru_cache(maxsize=4)
def _index(rows):
    from .catalog import _normalized

    postings = defaultdict(set)
    for pk, name, full_name in rows:
        for word in set(_normalized(f"{name or ''} {full_name or ''}").split()):
            if len(word) < 5:
                postings["e:" + word].add(pk)
            else:
                postings["p4:" + word[:4]].add(pk)
                if len(word) == 5:
                    postings["s4:" + word[:4]].add(pk)
                else:
                    postings["p5:" + word[:5]].add(pk)
    return postings


def rank_names(rows, stems):
    # Сам снимок имён — ключ кэша: bulk_update и смена активных товаров
    # инвалидируют индекс без сигналов Django и ручного сброса после импорта.
    postings = _index(tuple(rows))
    hits = Counter()
    for stem in dict.fromkeys(stems):
        if len(stem) < 5:
            keys = ["e:" + stem]
        elif len(stem) == 5:
            keys = ["p4:" + stem[:4]]
        else:
            keys = ["s4:" + stem[:4], "p5:" + stem[:5]]
        matches = set().union(*(postings.get(key, ()) for key in keys))
        hits.update(matches)
    return sorted(((count, pk) for pk, count in hits.items()), key=lambda row: (-row[0], row[1]))
