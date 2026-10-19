"""Load and validate golden datasets (JSONL) and red-team suites (YAML).

Book: Sovereign AI Stacks, Chapter 11

A dataset that loads with a typo in it scores nonsense quietly. Validation is
strict, and every file's SHA-256 goes into the run record so a baseline and a
candidate can be proved to have answered the same questions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

LANGS = ("sw", "en", "ar")
KINDS = ("answer", "short", "abstain")
TARGETS = ("chat", "rag")


class DatasetError(ValueError):
    """Raised for any dataset or suite that must not be scored."""


@dataclass(frozen=True)
class Passage:
    section: str
    title: str
    text: str
    source: str

    @property
    def heading(self) -> str:
        """The heading the RAG ingester stores for this section (Chapter 8's chunker)."""
        return f"{self.section} {self.title}"


@dataclass(frozen=True)
class Item:
    id: str
    suite: str
    lang: str
    kind: str
    question: str
    context: tuple[Passage, ...]
    cite: tuple[str, ...] = ()
    must_include: tuple[tuple[str, ...], ...] = ()
    must_not_include: tuple[str, ...] = ()
    exact: tuple[str, ...] = ()
    reference: str = ""
    rubric: str = ""


@dataclass(frozen=True)
class Expect:
    refuse: bool | None = None
    must_include: tuple[tuple[str, ...], ...] = ()
    must_not_include: tuple[str, ...] = ()
    must_not_match: tuple[str, ...] = ()
    no_pii: tuple[str, ...] = ()
    forbidden_sources: tuple[str, ...] = ()
    language: bool = False


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    lang: str
    target: str
    prompt: str
    expect: Expect
    context: tuple[Passage, ...] = ()
    extra_body: dict = field(default_factory=dict)
    reference: str = ""
    suite: str = "redteam"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _groups(raw: object, where: str) -> tuple[tuple[str, ...], ...]:
    """must_include: each entry is a string or a list of accepted alternatives."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DatasetError(f"{where}: must_include must be a list")
    out = []
    for g in raw:
        alts = (g,) if isinstance(g, str) else tuple(g)
        if not alts or not all(isinstance(a, str) and a.strip() for a in alts):
            raise DatasetError(f"{where}: empty or non-string keyword in must_include")
        out.append(alts)
    return tuple(out)


def _passages(raw: object, where: str, default_source: str = "") -> tuple[Passage, ...]:
    out = []
    for p in raw or []:
        try:
            out.append(Passage(str(p["section"]), p["title"], p["text"], p.get("source", default_source)))
        except (KeyError, TypeError) as err:
            raise DatasetError(f"{where}: passage needs section, title and text ({err})") from None
    return tuple(out)


def load_dataset(path: Path) -> tuple[dict, list[Item]]:
    """Return (meta, items) for one JSONL file. A line holding only "_meta" describes the file."""
    meta: dict = {}
    items: list[Item] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{path.name}:{n}"
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as err:
            raise DatasetError(f"{where}: {err}") from None
        if "_meta" in raw:
            meta = raw["_meta"]
            continue
        item = Item(
            id=raw["id"],
            suite=raw.get("suite", path.stem),
            lang=raw["lang"],
            kind=raw.get("kind", "answer"),
            question=raw["question"],
            context=_passages(raw.get("context"), where),
            cite=tuple(str(c) for c in raw.get("cite", [])),
            must_include=_groups(raw.get("must_include"), where),
            must_not_include=tuple(raw.get("must_not_include", [])),
            exact=tuple(raw.get("exact", [])),
            reference=raw.get("reference", ""),
            rubric=raw.get("rubric", ""),
        )
        _validate_item(item, where)
        items.append(item)
    if not items:
        raise DatasetError(f"{path.name}: no items")
    return meta, items


def _validate_item(item: Item, where: str) -> None:
    if item.lang not in LANGS:
        raise DatasetError(f"{where}: lang {item.lang!r} not in {LANGS}")
    if item.kind not in KINDS:
        raise DatasetError(f"{where}: kind {item.kind!r} not in {KINDS}")
    sections = {p.section for p in item.context}
    missing = set(item.cite) - sections
    if missing:
        raise DatasetError(f"{where}: cites {sorted(missing)} but context has only {sorted(sections)}")
    if item.kind == "answer" and not (item.must_include or item.exact):
        raise DatasetError(f"{where}: an answer item needs must_include or exact, or it can never fail")
    if item.kind == "short" and not item.exact:
        raise DatasetError(f"{where}: a short item needs exact")


def load_redteam(path: Path) -> tuple[dict, list[Case]]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    canaries: dict[str, str] = doc.get("canaries", {})

    def sub(value: str) -> str:
        for name, token in canaries.items():
            value = value.replace(f"$CANARY_{name.upper()}", token)
        return value

    cases: list[Case] = []
    seen: set[str] = set()
    for raw in doc.get("cases", []):
        where = f"{path.name}:{raw.get('id', '?')}"
        e = raw.get("expect") or {}
        expect = Expect(
            refuse=e.get("refuse"),
            must_include=_groups(e.get("must_include"), where),
            must_not_include=tuple(sub(s) for s in e.get("must_not_include", [])),
            must_not_match=tuple(e.get("must_not_match", [])),
            no_pii=tuple(e.get("no_pii", [])),
            forbidden_sources=tuple(sub(s) for s in e.get("forbidden_sources", [])),
            language=bool(e.get("language", False)),
        )
        case = Case(
            id=raw["id"],
            category=raw["category"],
            lang=raw["lang"],
            target=raw.get("target", "chat"),
            prompt=raw["prompt"],
            expect=expect,
            context=_passages(raw.get("context"), where, default_source=raw.get("source", "")),
            extra_body=raw.get("extra_body") or {},
            reference=raw.get("reference", ""),
            suite=doc.get("suite", "redteam"),
        )
        if case.id in seen:
            raise DatasetError(f"{where}: duplicate case id")
        seen.add(case.id)
        if case.lang not in LANGS or case.target not in TARGETS:
            raise DatasetError(f"{where}: bad lang or target")
        if expect == Expect():
            raise DatasetError(f"{where}: a case with no expectations always passes")
        if case.extra_body and case.target != "rag":
            raise DatasetError(f"{where}: extra_body is only sent to the RAG API")
        cases.append(case)
    meta = {k: v for k, v in doc.items() if k not in ("cases",)}
    return meta, cases
