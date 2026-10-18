#!/usr/bin/env python3
"""report.py — turn `vllm bench serve` results into a procurement-grade table.

Book: Sovereign AI Stacks, Chapter 5
Requires: Python 3.13 (standard library only)

    python report.py results/c*.json --slo-ttft-p95-ms 3000 --slo-tpot-p95-ms 100

Each result file is one concurrency level. The report states, for each level,
whether it meets the service-level objective (SLO) the requirements document
specified (Chapter 2), and names the highest concurrency that does.
Exit code: 0 if at least one level meets the SLO, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Level:
    concurrency: int
    completed: int
    request_throughput: float
    output_throughput: float
    p50_ttft_ms: float
    p95_ttft_ms: float
    p95_tpot_ms: float
    p99_e2el_ms: float

    @property
    def per_stream_tps(self) -> float:
        """Tokens/s one user sees while streaming: 1000 / time-per-output-token."""
        return 1000.0 / self.p95_tpot_ms if self.p95_tpot_ms > 0 else 0.0


def load(path: Path) -> Level:
    d = json.loads(path.read_text(encoding="utf-8"))
    try:
        return Level(
            concurrency=int(d["max_concurrency"]),
            completed=int(d["completed"]),
            request_throughput=float(d["request_throughput"]),
            output_throughput=float(d["output_throughput"]),
            p50_ttft_ms=float(d["p50_ttft_ms"]),
            p95_ttft_ms=float(d["p95_ttft_ms"]),
            p95_tpot_ms=float(d["p95_tpot_ms"]),
            p99_e2el_ms=float(d["p99_e2el_ms"]),
        )
    except KeyError as missing:
        raise SystemExit(f"{path}: missing {missing}; run vllm bench with --metric-percentiles 50,95,99") from None


def render(levels: list[Level], slo_ttft: float, slo_tpot: float) -> tuple[str, Level | None]:
    lines = [
        f"SLO: p95 time-to-first-token <= {slo_ttft:.0f} ms, p95 time-per-output-token <= {slo_tpot:.0f} ms",
        "",
        (
            "| Concurrency | Done | Req/s | Output tok/s | p50 TTFT ms | p95 TTFT ms "
            "| p95 TPOT ms | Stream tok/s | p99 E2E s | SLO |"
        ),
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    best: Level | None = None
    for lv in sorted(levels, key=lambda x: x.concurrency):
        ok = lv.p95_ttft_ms <= slo_ttft and lv.p95_tpot_ms <= slo_tpot
        if ok:
            best = lv
        lines.append(
            f"| {lv.concurrency} | {lv.completed} | {lv.request_throughput:.2f} | {lv.output_throughput:,.0f} | "
            f"{lv.p50_ttft_ms:,.0f} | {lv.p95_ttft_ms:,.0f} | {lv.p95_tpot_ms:.1f} | {lv.per_stream_tps:.1f} | "
            f"{lv.p99_e2el_ms / 1000:.1f} | {'PASS' if ok else 'FAIL'} |"
        )
    lines.append("")
    if best:
        lines.append(
            f"Highest concurrency meeting the SLO: {best.concurrency} "
            f"({best.output_throughput:,.0f} output tok/s aggregate, {best.per_stream_tps:.1f} tok/s per stream)."
        )
    else:
        lines.append("No tested concurrency meets the SLO.")
    return "\n".join(lines), best


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("results", nargs="+", type=Path)
    p.add_argument("--slo-ttft-p95-ms", type=float, default=3000)
    p.add_argument("--slo-tpot-p95-ms", type=float, default=100)
    args = p.parse_args(argv)
    text, best = render([load(r) for r in args.results], args.slo_ttft_p95_ms, args.slo_tpot_p95_ms)
    print(text)
    return 0 if best else 1


if __name__ == "__main__":
    sys.exit(main())
