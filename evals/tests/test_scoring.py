"""Scorers, normalisation and the golden sets themselves.

Book: Sovereign AI Stacks, Chapter 11
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from evalkit.datasets import DatasetError, load_dataset, load_redteam
from evalkit.mock import EVALS_DIR
from evalkit.runner import SYSTEM_PROMPT
from evalkit.scoring import Source, score_case, score_item
from evalkit.text import cited_numbers, contains, detect_language, find_pii, is_refusal, normalise

DATASETS = sorted((EVALS_DIR / "datasets").glob("*.jsonl"))
ITEMS = [i for f in DATASETS for i in load_dataset(f)[1]]
CASES = load_redteam(EVALS_DIR / "redteam" / "injection-suite.yaml")[1]


def _sources(item):
    return [Source(p.section, p.source) for p in item.context]


# --- text ----------------------------------------------------------------------
def test_arabic_normalisation_folds_marks_hamza_and_digits():
    assert normalise("شهرًا") == normalise("شهرا")
    assert normalise("إجازة") == normalise("اجازه")
    assert contains(normalise("مدتها ٢٨ يوم عمل"), "28")


def test_numbers_do_not_match_inside_other_numbers():
    assert contains(normalise("ndani ya siku 5"), "5")
    assert not contains(normalise("ndani ya siku 15"), "5")
    assert not contains(normalise("siku 50"), "5")


def test_affixed_roots_match():
    assert contains(normalise("Lazima uiripoti zawadi hiyo"), "ripoti")
    assert contains(normalise("بموافقة خطية"), "موافقة")


def test_citation_markers_never_satisfy_numeric_keywords():
    item = next(i for i in ITEMS if i.id == "en-004")  # needs "3"
    assert not score_item(item, "You need a certificate from a doctor [2] [3].", _sources(item))["must_include"]


def test_cited_numbers_handles_lists_and_arabic_digits():
    assert cited_numbers("A [1, 2] then [2] and [١]") == [1, 2]


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("Una haki ya likizo ya mwaka ya siku 28 za kazi [1].", "sw"),
        ("You are entitled to 28 working days of annual leave [1].", "en"),
        ("تستحق إجازة سنوية مدتها 28 يوم عمل [1].", "ar"),
        ("MOH-T3", "unknown"),
    ],
)
def test_detect_language(text, lang):
    assert detect_language(text) == lang


def test_refusals_in_three_languages():
    assert is_refusal("I cannot find this in the documents available to you.")
    assert is_refusal("Siwezi kukusaidia kughushi cheti.")
    assert is_refusal("لا أستطيع مشاركة أرقام الهوية.")
    assert not is_refusal("Annual leave cannot be carried over to the next year.")


def test_pii_patterns():
    assert find_pii("NIDA 19840312-11223-00001-25", ["nida"]) == ["nida"]
    assert find_pii("NIDA 19840312 11223 00001 25", ["nida"]) == ["nida"]
    assert find_pii("784‑1984‑1234567‑1", ["emirates_id"]) == ["emirates_id"]  # non-breaking hyphens
    assert find_pii("extension 214, room 1984", ["nida", "emirates_id"]) == []


# --- the golden sets -------------------------------------------------------------
def test_every_language_has_the_same_parallel_items():
    by_lang: dict[str, list] = {}
    for i in ITEMS:
        by_lang.setdefault(i.lang, []).append(i)
    assert set(by_lang) == {"sw", "en", "ar"}
    shape = {lang: [(i.id[3:], i.kind, tuple(p.section for p in i.context), i.cite) for i in v]
             for lang, v in by_lang.items()}
    assert shape["sw"] == shape["en"] == shape["ar"]
    assert all(len(v) >= 12 for v in by_lang.values())


@pytest.mark.parametrize("item", ITEMS, ids=lambda i: i.id)
def test_reference_answer_passes_its_own_scorer(item):
    # A golden answer that fails its own checks means the item is wrong, not the model.
    checks = score_item(item, item.reference, _sources(item))
    assert all(checks.values()), checks


@pytest.mark.parametrize("item", [i for i in ITEMS if i.kind == "answer"], ids=lambda i: i.id)
def test_distractor_passage_does_not_pass(item):
    # Pasting the wrong section back must fail: the keywords discriminate.
    wrong = next(p for p in item.context if p.section not in item.cite)
    assert not all(score_item(item, f"{wrong.text} [1]", _sources(item)).values())


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_reference_behaviour_passes_each_case(case):
    sources = [Source(p.section, p.source) for p in case.context]
    checks = score_case(case, case.reference, sources)
    assert checks and all(checks.values()), checks


def test_validation_rejects_a_citation_outside_the_context(tmp_path: Path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        '{"id":"x-1","lang":"en","question":"q?","context":[{"section":"1.1","title":"t","text":"x"}],'
        '"cite":["9.9"],"must_include":["x"]}\n',
        encoding="utf-8",
    )
    with pytest.raises(DatasetError, match="cites"):
        load_dataset(bad)


def test_system_prompt_matches_the_rag_service():
    api = Path(__file__).resolve().parents[2] / "apps" / "rag" / "src" / "rag" / "api.py"
    if not api.exists():
        pytest.skip("RAG service source not present (image build)")
    tree = ast.parse(api.read_text(encoding="utf-8"))
    prompt = next(
        ast.literal_eval(n.value) for n in tree.body
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "SYSTEM_PROMPT"
    )
    assert prompt == SYSTEM_PROMPT
