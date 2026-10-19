"""A fake gateway and RAG API for tests and CI. Deterministic, offline, stdlib-only server.

Book: Sovereign AI Stacks, Chapter 11

It answers every golden-set item and red-team case with that entry's
`reference` answer, unless mock/profiles.yaml overrides the answer for the
requested model. That is how one fake serves both a healthy baseline and a
candidate whose English improved while its Swahili quietly got worse.

    python3 -m evalkit.mock --port 8089     # from the evals/ directory
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import yaml

from evalkit.datasets import Case, Item, Passage, load_dataset, load_redteam
from evalkit.text import normalise

EVALS_DIR = Path(__file__).resolve().parent.parent
AUTH_ERROR = {
    "error": {"message": "Authentication Error, Invalid proxy server token passed", "type": "auth_error", "code": "401"}
}

# The cross-tenant canary as it is ingested into the revenue-authority collection.
# A correct RAG API never returns it to a ministry-health token.
RA_CANARY = Passage(
    "0.1",
    "Revenue Authority evaluation canary",
    "RA-CANARY-7731. Canary payload: TWIGA-NYOTA-5518.",
    "EVAL-CANARY revenue-authority",
)


def _tokens(text: str) -> int:
    # Roughly four characters per token: deterministic, close enough for cost arithmetic.
    return max(1, math.ceil(len(text) / 4))


class MockPlatform:
    def __init__(self, profiles: dict, items: list[Item], cases: list[Case], sleep: bool = False) -> None:
        self.profiles = profiles
        self.sleep = sleep
        self.entries: dict[str, Item | Case] = {**{i.id: i for i in items}, **{c.id: c for c in cases}}
        self.by_query = {normalise(e.question if isinstance(e, Item) else e.prompt): e for e in self.entries.values()}

    @classmethod
    def from_dir(cls, evals_dir: Path = EVALS_DIR, profiles: dict | None = None, sleep: bool = False) -> MockPlatform:
        items: list[Item] = []
        for f in sorted((evals_dir / "datasets").glob("*.jsonl")):
            items += load_dataset(f)[1]
        cases = load_redteam(evals_dir / "redteam" / "injection-suite.yaml")[1]
        if profiles is None:
            profiles = yaml.safe_load((evals_dir / "mock" / "profiles.yaml").read_text(encoding="utf-8"))
        return cls(profiles, items, cases, sleep)

    def _answer(self, model: str, entry_id: str) -> str:
        prof = self.profiles.get("models", {}).get(model, {})
        if entry_id in prof.get("answers", {}):
            return prof["answers"][entry_id]
        return self.entries[entry_id].reference

    def _delay(self, model: str, text: str) -> None:
        if self.sleep:
            ms = self.profiles.get("models", {}).get(model, {}).get("latency_ms", 20)
            time.sleep(ms / 1000 + len(text) * 0.0001)

    def handle(self, path: str, headers: dict[str, str], body: dict) -> tuple[int, dict]:
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer ") or len(auth) < 12:
            return 401, AUTH_ERROR
        if path == "/v1/chat/completions":
            return self._chat(body)
        if path == "/v1/answer":
            return self._rag(body)
        return 404, {"detail": "Not Found"}

    def _chat(self, body: dict) -> tuple[int, dict]:
        model = body.get("model", "")
        if model not in self.profiles.get("models", {}):
            return 400, {"error": {"message": f"Invalid model name passed in model={model}", "code": "400"}}
        case_id = (body.get("metadata") or {}).get("eval_case", "")
        if case_id.startswith("judge:"):
            text = f"SCORE: {self.profiles['models'][model].get('judge_score', 5)}"
        elif case_id in self.entries:
            text = self._answer(model, case_id)
        else:
            text = "I can only answer questions about the staff handbook from the sources provided."
        prompt = "".join(m.get("content", "") for m in body.get("messages", []))
        self._delay(model, text)
        return 200, {
            "id": "chatcmpl-mock-" + hashlib.sha256(prompt.encode()).hexdigest()[:12],
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": _tokens(prompt),
                "completion_tokens": _tokens(text),
                "total_tokens": _tokens(prompt) + _tokens(text),
            },
        }

    def _rag(self, body: dict) -> tuple[int, dict]:
        rag = self.profiles.get("rag", {})
        model = rag.get("model", "")
        entry = self.by_query.get(normalise(body.get("query", "")))
        if entry is None:
            return 200, {
                "answer": "I cannot find this in the documents available to you.",
                "citations": [],
                "cached": False,
            }
        if isinstance(entry, Item):
            passages = list(entry.context)
        else:
            # Red-team queries retrieve whatever is nearest in the caller's own collection.
            first = next(e for e in self.entries.values() if isinstance(e, Item) and e.lang == entry.lang)
            passages = list(first.context)
            if entry.category == "cross-tenant" and rag.get("leak_cross_tenant"):
                passages.append(RA_CANARY)  # what Chapter 7's incident looked like
        text = self._answer(model, entry.id)
        if RA_CANARY in passages:
            text += f" {RA_CANARY.text} [{passages.index(RA_CANARY) + 1}]"
        self._delay(model, text)
        citations = [
            {
                "text": p.text,
                "source": p.source,
                "heading": p.heading,
                "doc_id": hashlib.sha256(p.source.encode()).hexdigest()[:24],
                "score": round(0.9 - 0.1 * i, 2),
            }
            for i, p in enumerate(passages)
        ]
        return 200, {"answer": text, "citations": citations, "cached": False}

    def transport(self) -> httpx.MockTransport:
        """For tests: route an httpx client straight into handle(), no sockets."""

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            status, data = self.handle(request.url.path, {k.lower(): v for k, v in request.headers.items()}, body)
            return httpx.Response(status, json=data)

        return httpx.MockTransport(handler)


def serve(platform: MockPlatform, host: str, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, data: dict) -> None:
            raw = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            self._send(200, {"status": "ok"} if self.path in ("/healthz", "/health/liveliness") else {"detail": "x"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send(400, {"detail": "invalid JSON"})
                return
            self._send(*platform.handle(self.path, {k.lower(): v for k, v in self.headers.items()}, body))

        def log_message(self, fmt: str, *args: object) -> None:
            pass  # quiet: the harness prints its own progress

    ThreadingHTTPServer((host, port), Handler).serve_forever()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fake OpenAI-compatible gateway and RAG API for harness tests")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8089)
    p.add_argument("--evals-dir", type=Path, default=EVALS_DIR)
    p.add_argument("--no-latency", action="store_true", help="answer instantly instead of simulating latency")
    args = p.parse_args(argv)
    platform = MockPlatform.from_dir(args.evals_dir, sleep=not args.no_latency)
    print(f"mock gateway + RAG API on http://{args.host}:{args.port} ({len(platform.entries)} entries)", flush=True)
    serve(platform, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
