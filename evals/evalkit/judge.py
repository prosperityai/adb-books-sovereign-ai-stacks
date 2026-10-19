"""OPTIONAL LLM-as-judge, called through the gateway. Advisory, never the gate by default.

Book: Sovereign AI Stacks, Chapter 11

A judge model grades fluency and faithfulness that keyword checks cannot see.
It is also a model with its own language gaps: a judge that is weak in Swahili
grades Swahili answers badly. The deterministic scorers decide acceptance;
the judge's mean score is reported beside them, and gates on it only if
criteria.yaml sets judge.min_mean.
"""

from __future__ import annotations

import re

from evalkit.client import Endpoints
from evalkit.datasets import Item

JUDGE_PROMPT = """You are grading an answer given by an assistant to a government employee.
The question is in language code "{lang}". The answer must be in the same language,
must be supported by the reference, and must not add facts that the reference lacks.

Question:
{question}

Reference answer:
{reference}

Grading notes:
{rubric}

Assistant's answer:
{answer}

Score from 1 (wrong, unsupported or wrong language) to 5 (correct, complete, faithful, same language).
Reply with exactly one line: SCORE: <1-5>"""

_SCORE = re.compile(r"SCORE:\s*([1-5])\b")


async def judge(endpoints: Endpoints, model: str, item: Item, answer: str, run_id: str) -> int | None:
    prompt = JUDGE_PROMPT.format(
        lang=item.lang, question=item.question, reference=item.reference, rubric=item.rubric, answer=answer
    )
    reply = await endpoints.chat(
        model,
        [{"role": "user", "content": prompt}],
        max_tokens=16,
        metadata={"tags": ["eval-judge", run_id], "eval_case": f"judge:{item.id}"},
    )
    if reply.error:
        return None
    m = _SCORE.search(reply.text)
    return int(m.group(1)) if m else None
