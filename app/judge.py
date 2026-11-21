"""Inspection judgment engine.

Given an immutable version definition and the answers stored on one record,
classify every item into one of four categories:

``answered``  已答   always displayed, original answer given
``missing``   漏填   always displayed, originally blank
``exempt``    免检   never displayed, whatever the blank answers become
``pending``   待核   sometimes displayed, sometimes not -> needs verification

Blank displayed answers are considered under both possible supplements (是/否),
but each item takes exactly one value in a single completion.  Answers on
items that are hidden in a completion never influence display decisions.

The engine evaluates every completion in parallel using Python integers as
bit masks: with at most 12 items there are at most 4096 completions, i.e. a
4096-bit mask.  This keeps per-record judgment cheap enough for 50,000 rows.

Preparing a version builds the display mask of every item once, using all
items as free variables.  A record is then judged with plain bit operations:
intersect the masks of its known answers to obtain the consistent completions,
then compare each item's display mask against that set.  No condition group is
walked per record.  Prepared plans are cached by the exact serialised rule set
with a bounded LRU, so imports and single-record corrections in the same
version share one plan and versions with different rules never share one.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from .version_def import dumps, loads

ANSWER_YES = "是"
ANSWER_NO = "否"

CATEGORIES = ("answered", "missing", "exempt", "pending")
CATEGORY_LABELS = {
    "answered": "已答",
    "missing": "漏填",
    "exempt": "免检",
    "pending": "待核",
}

_MASK_CACHE: "OrderedDict[int, tuple[int, list[int]]]" = OrderedDict()
_MASK_CACHE_LIMIT = 16
_MASK_CACHE_LOCK = threading.Lock()

PLAN_CACHE_LIMIT = 16
_PLAN_CACHE: "OrderedDict[str, PreparedRules]" = OrderedDict()
_PLAN_CACHE_LOCK = threading.Lock()


def _masks(variable_count: int) -> tuple[int, list[int]]:
    """Return (all_ones, true_masks) for ``variable_count`` free variables."""
    with _MASK_CACHE_LOCK:
        cached = _MASK_CACHE.get(variable_count)
        if cached is not None:
            _MASK_CACHE.move_to_end(variable_count)
            return cached

    total = 1 << variable_count
    all_ones = (1 << total) - 1
    true_masks: list[int] = []
    for variable in range(variable_count):
        block = 1 << variable
        chunk = (1 << block) - 1
        mask = 0
        position = 0
        while position < total:
            mask |= chunk << (position + block)
            position += 2 * block
        true_masks.append(mask)

    result = (all_ones, true_masks)
    with _MASK_CACHE_LOCK:
        _MASK_CACHE[variable_count] = result
        _MASK_CACHE.move_to_end(variable_count)
        while len(_MASK_CACHE) > _MASK_CACHE_LIMIT:
            _MASK_CACHE.popitem(last=False)
    return result


class PreparedRules:
    """Display masks for one exact rule set, reusable across every record.

    ``shown_masks[i]`` has one bit per completion of all items (是=1, 否=0).
    Judging a record only intersects those masks with the completions that
    agree with its known answers; no condition group is walked again.
    """

    __slots__ = (
        "item_count",
        "all_ones",
        "yes_masks",
        "no_masks",
        "shown_masks",
    )

    def __init__(self, items: list[dict]):
        item_count = len(items)
        all_ones, yes_masks = _masks(item_count)
        shown_masks: list[int] = [0] * item_count

        for index, item in enumerate(items):
            if item["unconditional"]:
                shown_masks[index] = all_ones
                continue

            shown_mask = 0
            for group in item["groups"]:
                group_mask = all_ones
                for reference, required in group:
                    reference_mask = shown_masks[reference]
                    if reference_mask == 0:
                        group_mask = 0
                        break
                    value_mask = (
                        yes_masks[reference]
                        if required == ANSWER_YES
                        else ~yes_masks[reference]
                    )
                    group_mask &= reference_mask & value_mask
                    if group_mask == 0:
                        break
                shown_mask |= group_mask

            shown_masks[index] = shown_mask & all_ones

        self.item_count = item_count
        self.all_ones = all_ones
        self.yes_masks = yes_masks
        self.no_masks = [mask ^ all_ones for mask in yes_masks]
        self.shown_masks = shown_masks

    def judge(self, answers: dict[int, str]) -> list[str]:
        """Classify each item under every completion consistent with answers."""
        consistent = self.all_ones
        yes_masks = self.yes_masks
        no_masks = self.no_masks
        for index, value in answers.items():
            if not 0 <= index < self.item_count:
                continue
            consistent &= (
                yes_masks[index] if value == ANSWER_YES else no_masks[index]
            )

        categories: list[str] = []
        for index, shown in enumerate(self.shown_masks):
            visible = shown & consistent
            if visible == 0:
                categories.append("exempt")
            elif visible == consistent:
                categories.append("answered" if index in answers else "missing")
            else:
                categories.append("pending")
        return categories


def prepare(definition: dict, key: str | None = None) -> PreparedRules:
    """Build or reuse the prepared rules for exactly this definition.

    ``key`` may be the definition text exactly as stored in the database, so
    loading the same version avoids re-serialising its rules.  Otherwise the
    key is the serialised rule set.  Either way, versions whose rules differ
    can never share a plan, and at most ``PLAN_CACHE_LIMIT`` plans are kept.
    """
    cache_key = dumps(definition) if key is None else key
    with _PLAN_CACHE_LOCK:
        cached = _PLAN_CACHE.get(cache_key)
        if cached is not None:
            _PLAN_CACHE.move_to_end(cache_key)
            return cached

    prepared = PreparedRules(definition["items"])

    with _PLAN_CACHE_LOCK:
        existing = _PLAN_CACHE.get(cache_key)
        if existing is not None:
            _PLAN_CACHE.move_to_end(cache_key)
            return existing
        _PLAN_CACHE[cache_key] = prepared
        _PLAN_CACHE.move_to_end(cache_key)
        while len(_PLAN_CACHE) > PLAN_CACHE_LIMIT:
            _PLAN_CACHE.popitem(last=False)
    return prepared


def judge(definition: dict, answers: dict[int, str]) -> list[str]:
    """Classify each item; ``answers`` maps item index to 是/否."""
    return prepare(definition).judge(answers)


def display_under_answers(definition: dict, answers: dict[int, str]) -> list[bool]:
    """Deterministic display using only the original answers (blanks absent)."""
    items = definition["items"]
    shown: list[bool] = []
    for item in items:
        if item["unconditional"]:
            shown.append(True)
            continue
        visible = False
        for group in item["groups"]:
            if all(
                shown[reference] and answers.get(reference) == required
                for reference, required in group
            ):
                visible = True
                break
        shown.append(visible)
    return shown


def count_categories(category_lists: list[list[str]]) -> dict[int, dict[str, int]]:
    """Aggregate categories into per-item counts."""
    counts: dict[int, dict[str, int]] = {}
    for categories in category_lists:
        for index, category in enumerate(categories):
            item_counts = counts.setdefault(
                index, {name: 0 for name in CATEGORIES}
            )
            item_counts[category] += 1
    return counts


__all__ = [
    "CATEGORIES",
    "CATEGORY_LABELS",
    "PLAN_CACHE_LIMIT",
    "PreparedRules",
    "judge",
    "prepare",
    "display_under_answers",
    "count_categories",
    "loads",
]
