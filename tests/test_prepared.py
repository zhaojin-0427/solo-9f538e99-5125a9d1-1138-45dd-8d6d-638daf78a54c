"""Prepared-rule cache tests."""

import itertools
import random

from app import judge as judge_module
from app.judge import PreparedRules, judge, prepare
from app.version_def import dumps, parse_definition


def definition(text):
    parsed, errors = parse_definition(text)
    assert errors == [], errors
    return parsed


def reference(definition, answers):
    items = definition["items"]
    blank = [i for i in range(len(items)) if i not in answers]
    shown_sets = []
    for combo in itertools.product(("是", "否"), repeat=len(blank)):
        completion = dict(answers)
        completion.update(dict(zip(blank, combo)))
        shown = []
        for item in items:
            if item["unconditional"]:
                shown.append(True)
                continue
            visible = False
            for group in item["groups"]:
                if all(
                    shown[ref] and completion.get(ref) == required
                    for ref, required in group
                ):
                    visible = True
                    break
            shown.append(visible)
        shown_sets.append(shown)

    categories = []
    for index in range(len(items)):
        values = {shown[index] for shown in shown_sets}
        if values == {False}:
            categories.append("exempt")
        elif values == {True}:
            categories.append("answered" if index in answers else "missing")
        else:
            categories.append("pending")
    return categories


MULTI_GROUP = """\
甲
乙
丙 | 1=是
丁 | 1=是 & 2=是
戊 | 1=否 ; 2=否
己 | 3=是 ; 4=是 & 5=否
庚 | 1=是 & 2=是 & 3=是
辛 | 6=是 & 7=否
"""


def test_prepared_matches_reference_for_varied_answers():
    d = definition(MULTI_GROUP)
    rules = prepare(d)
    rng = random.Random(20261121)
    for _ in range(500):
        answers = {}
        for index in range(len(d["items"])):
            roll = rng.random()
            if roll < 0.4:
                answers[index] = "是"
            elif roll < 0.7:
                answers[index] = "否"
        assert rules.judge(answers) == reference(d, answers)
        assert judge(d, answers) == reference(d, answers)


def test_same_rules_reuse_one_prepared_plan():
    first = definition(MULTI_GROUP)
    second = definition(MULTI_GROUP)
    assert prepare(first) is prepare(second)


def test_different_rules_never_share_a_plan():
    yes = definition("甲\n乙 | 1=是")
    no = definition("甲\n乙 | 1=否")
    assert prepare(yes) is not prepare(no)
    assert prepare(yes).judge({0: "是", 1: "是"}) == ["answered", "answered"]
    assert prepare(no).judge({0: "是", 1: "是"}) == ["answered", "exempt"]


def test_plan_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(judge_module, "PLAN_CACHE_LIMIT", 4)
    judge_module._PLAN_CACHE.clear()
    for index in range(12):
        d = definition(f"甲\n乙\n丙 | {1 + index % 2}=是")
        prepare(d)
    assert len(judge_module._PLAN_CACHE) <= 4


def test_mask_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(judge_module, "_MASK_CACHE_LIMIT", 3)
    judge_module._MASK_CACHE.clear()
    for count in range(1, 10):
        text = "\n".join(f"项{i}" for i in range(count))
        prepare(definition(text))
    assert len(judge_module._MASK_CACHE) <= 3


def test_judge_uses_only_prepared_masks_not_groups():
    d = definition(MULTI_GROUP)
    rules = PreparedRules(d["items"])
    # Mutating the rule text must not change an already prepared judgment.
    for item in d["items"]:
        item["groups"] = [[[0, "否"]]]
        item["unconditional"] = False
    answers = {0: "是", 1: "否"}
    assert rules.judge(answers) == reference(definition(MULTI_GROUP), answers)


def test_cache_key_accepts_stored_definition_text():
    d = definition(MULTI_GROUP)
    raw = dumps(d)
    assert prepare(d, raw) is prepare(definition(MULTI_GROUP), raw)
    assert prepare(d, raw) is prepare(definition(MULTI_GROUP))
