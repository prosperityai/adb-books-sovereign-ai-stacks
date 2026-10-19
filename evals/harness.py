#!/usr/bin/env python3
"""Evaluation and acceptance harness for the sovereign LLM platform.

Book: Sovereign AI Stacks, Chapter 11
Requires: Python 3.13, httpx 0.28.1, PyYAML 6.0.3 (see pyproject.toml)

    run     ask every golden-set item and red-team case; write run.json + scorecard.md
    gate    judge a candidate run against acceptance/criteria.yaml and a baseline run;
            write gate.json + gate.md; exit 1 if any gate fails
    corpus  write the golden-set passages as Markdown handbooks for RAG ingestion

Secrets come from the environment, never flags (flags end up in shell history
and in `ps` output):
    EVAL_GATEWAY_KEY        the eval-harness virtual key (Chapter 6)
    EVAL_RAG_TOKEN          a RAG API bearer token, or instead:
    EVAL_OIDC_TOKEN_URL, EVAL_RAG_CLIENT_ID, EVAL_RAG_CLIENT_SECRET
                            Keycloak client credentials (Chapter 7)

Exit codes: 0 success / gate passed, 1 gate failed, 2 bad input or configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import yaml
from evalkit.client import Endpoints, client_credentials_token
from evalkit.datasets import DatasetError, Item, load_dataset, load_redteam, sha256_file
from evalkit.gate import evaluate, write_gate
from evalkit.report import summarise, write_run
from evalkit.runner import run_all

HERE = Path(__file__).resolve().parent
DEFAULT_GATEWAY = "http://litellm.ai-gateway.svc:4000"


def _die(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 2


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _rag_auth(env: dict[str, str]):
    """Return (token, refresh) for the RAG API from the environment, or (None, None)."""
    if env.get("EVAL_RAG_TOKEN"):
        return env["EVAL_RAG_TOKEN"], None
    url, cid, secret = (env.get(k) for k in ("EVAL_OIDC_TOKEN_URL", "EVAL_RAG_CLIENT_ID", "EVAL_RAG_CLIENT_SECRET"))
    if url and cid and secret:
        return None, lambda: client_credentials_token(url, cid, secret)
    return None, None


async def _run(args: argparse.Namespace, env: dict[str, str], transport=None) -> int:
    key = env.get("EVAL_GATEWAY_KEY")
    if not key:
        return _die("EVAL_GATEWAY_KEY is not set (the eval-harness virtual key)")
    datasets = args.dataset or sorted((HERE / "datasets").glob("*.jsonl"))
    items: list[Item] = []
    provenance: dict[str, dict] = {}
    try:
        for path in datasets:
            meta, loaded = load_dataset(Path(path))
            items += loaded
            provenance[Path(path).name] = {"sha256": sha256_file(Path(path)), "version": meta.get("version")}
        cases = []
        canary = ""
        if not args.no_redteam:
            rt_meta, cases = load_redteam(args.redteam)
            canary = rt_meta.get("canaries", {}).get("system", "")
            provenance[args.redteam.name] = {"sha256": sha256_file(args.redteam), "version": rt_meta.get("version")}
    except (DatasetError, OSError, KeyError) as err:
        return _die(str(err))
    ids = [x.id for x in items] + [c.id for c in cases]
    if len(ids) != len(set(ids)):
        return _die("duplicate item ids across datasets")

    token, refresh = _rag_auth(env)
    if token is None and refresh is not None:
        token = await refresh()
    needs_rag = args.qa_target == "rag" or any(c.target == "rag" for c in cases)
    if needs_rag and not (args.rag_url and token):
        print("warning: RAG target not configured; RAG cases will be recorded as errors", file=sys.stderr)

    ep = Endpoints(
        args.gateway_url,
        key,
        args.rag_url,
        token,
        refresh,
        transport=transport,
        max_retries=args.max_retries,
        backoff_s=args.backoff,
    )
    try:
        run = await run_all(
            ep,
            args.model,
            items,
            cases,
            qa_target=args.qa_target,
            concurrency=args.concurrency,
            judge_model=args.judge_model,
            canary=canary,
            run_id=args.run_id,
            progress=None if args.quiet else lambda line: print(line, file=sys.stderr),
        )
    finally:
        await ep.aclose()
    run["datasets"] = provenance
    run["endpoints"] = {"gateway": args.gateway_url, "rag": args.rag_url}
    pricing = _load_yaml(args.criteria).get("cost", {}) if args.criteria.exists() else {}
    run["summary"] = summarise(run, pricing)
    write_run(run, args.out)
    _print_summary(run)
    print(f"wrote {args.out / 'run.json'} and {args.out / 'scorecard.md'}")
    return 0


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _print_summary(run: dict) -> None:
    s = run["summary"]
    print(f"run {run['run_id']}  model={run['model']}  target={run['qa_target']}")
    print(f"{'lang':<5} {'items':>5} {'accuracy':>9} {'citations':>9} {'language':>9} {'abstain':>8} {'p95_s':>7}")
    for lang, st in [*s["per_language"].items(), ("all", s["overall"])]:
        print(
            f"{lang:<5} {st['items']:>5} {_pct(st['accuracy']):>9} {_pct(st['citation_rate']):>9} "
            f"{_pct(st['language_match']):>9} {_pct(st['abstain_accuracy']):>8} {st['latency_p95_s']:>7}"
        )
    rt = s["redteam"]
    if rt["cases"]:
        print(f"red team: {rt['passed']}/{rt['cases']} passed")
    if s["overall"]["cached"]:
        print(
            f"warning: {s['overall']['cached']} RAG answers came from the answer cache; "
            "restart rag-api or wait RAG_CACHE_TTL before measuring"
        )


def _gate(args: argparse.Namespace) -> int:
    try:
        candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
        baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
        criteria = _load_yaml(args.criteria)
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as err:
        return _die(str(err))
    results = evaluate(candidate, criteria, baseline)
    passed = write_gate(candidate, baseline, criteria, results, args.out)
    for g in results:
        if not g.passed or args.verbose:
            mark = "PASS" if g.passed else "FAIL"
            print(f"{mark}  {g.gate:<26} {g.scope:<22} value={g.value} threshold={g.threshold}  {g.detail}".rstrip())
    n_fail = sum(not g.passed for g in results)
    print(f"verdict: {'ACCEPT' if passed else 'REJECT'} ({len(results) - n_fail}/{len(results)} gates passed)")
    print(f"wrote {args.out / 'gate.json'} and {args.out / 'gate.md'}")
    return 0 if passed else 1


def _corpus(args: argparse.Namespace) -> int:
    """One Markdown handbook per language. Headings become the chunk headings the RAG API cites."""
    datasets = args.dataset or sorted((HERE / "datasets").glob("*.jsonl"))
    by_lang: dict[str, dict[str, tuple[str, str, str]]] = {}
    for path in datasets:
        for item in load_dataset(Path(path))[1]:
            for p in item.context:
                by_lang.setdefault(item.lang, {})[p.section] = (p.title, p.text, p.source)
    args.out.mkdir(parents=True, exist_ok=True)
    for lang, sections in sorted(by_lang.items()):
        source = next(iter(sections.values()))[2]
        ordered = sorted(sections.items(), key=lambda kv: tuple(int(x) for x in kv[0].split(".")))
        body = [f"# {source}", ""] + [f"## {sec} {title}\n\n{text}\n" for sec, (title, text, _) in ordered]
        path = args.out / f"mohh-handbook-{lang}.md"
        path.write_text("\n".join(body), encoding="utf-8")
        print(f'{path}: {len(sections)} sections, ingest with --source "{source}"')
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run golden sets and the red-team suite against a model")
    r.add_argument("--model", required=True, help="gateway model alias, e.g. qwen3.8-27b")
    r.add_argument("--gateway-url", default=os.environ.get("EVAL_GATEWAY_URL", DEFAULT_GATEWAY))
    r.add_argument("--rag-url", default=os.environ.get("EVAL_RAG_URL"), help="e.g. http://rag-api.rag.svc:8000")
    r.add_argument("--dataset", action="append", type=Path, help="JSONL golden set (repeatable; default: all)")
    r.add_argument("--redteam", type=Path, default=HERE / "redteam" / "injection-suite.yaml")
    r.add_argument("--no-redteam", action="store_true")
    r.add_argument("--qa-target", choices=["chat", "rag"], default="chat")
    r.add_argument("--concurrency", type=int, default=4, help="stay below the key's max_parallel_requests (16)")
    r.add_argument("--judge-model", help="OPTIONAL: grade answers with this gateway model (advisory)")
    r.add_argument("--criteria", type=Path, default=HERE / "acceptance" / "criteria.yaml", help="for pricing")
    r.add_argument("--run-id")
    r.add_argument("--max-retries", type=int, default=3)
    r.add_argument("--backoff", type=float, default=1.0, help="first retry delay in seconds, doubling")
    r.add_argument("--quiet", action="store_true")
    r.add_argument("--out", type=Path, required=True)

    g = sub.add_parser("gate", help="apply acceptance criteria; exit 1 on any failed gate")
    g.add_argument("--candidate", type=Path, required=True, help="run.json of the model under test")
    g.add_argument("--baseline", type=Path, help="run.json of the model in production (omit at first acceptance)")
    g.add_argument("--criteria", type=Path, default=HERE / "acceptance" / "criteria.yaml")
    g.add_argument("--verbose", action="store_true", help="print passing gates too")
    g.add_argument("--out", type=Path, required=True)

    c = sub.add_parser("corpus", help="export the golden-set passages as Markdown for RAG ingestion")
    c.add_argument("--dataset", action="append", type=Path)
    c.add_argument("--out", type=Path, required=True)
    return p


def main(argv: list[str] | None = None, env: dict[str, str] | None = None, transport=None) -> int:
    args = build_parser().parse_args(argv)
    env = dict(os.environ) if env is None else env
    if args.command == "run":
        if args.concurrency < 1:
            return _die("--concurrency must be at least 1")
        return asyncio.run(_run(args, env, transport))
    if args.command == "gate":
        return _gate(args)
    return _corpus(args)


if __name__ == "__main__":
    raise SystemExit(main())
