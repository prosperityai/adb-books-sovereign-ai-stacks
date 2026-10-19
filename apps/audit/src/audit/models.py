"""LiteLLM generic_api payloads in, redacted audit records out.

Book: Sovereign AI Stacks, Chapter 10

LiteLLM's generic_api callback (litellm/integrations/generic_api) posts
StandardLoggingPayload objects. Its default log_format is "json_array", a
batch of up to DEFAULT_BATCH_SIZE (512) objects flushed every
DEFAULT_FLUSH_INTERVAL_SECONDS (5); "single" posts one object per request and
"ndjson" one object per line. parse_body accepts all three.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from audit.redact import Redactor, injection_signals, strings_in


class PayloadError(ValueError):
    """The request body is not a LiteLLM logging payload."""


@dataclass(frozen=True)
class Retention:
    metadata_days: int
    content_days: int


@dataclass(frozen=True)
class RetentionPolicy:
    """Per-tenant retention, read from AUDIT_RETENTION_DAYS.

    {"*": {"metadata": 730, "content": 180}, "revenue-authority": {"metadata": 3650, "content": 365}}
    A plain number sets both clocks. "*" is required: it covers unknown tenants.
    """

    tenants: dict[str, Retention]

    @staticmethod
    def from_json(text: str) -> RetentionPolicy:
        raw = json.loads(text)
        if not isinstance(raw, dict) or "*" not in raw:
            raise ValueError('retention policy must be a JSON object with a "*" default')
        out: dict[str, Retention] = {}
        for tenant, v in raw.items():
            if isinstance(v, dict):
                r = Retention(int(v["metadata"]), int(v["content"]))
            else:
                r = Retention(int(v), int(v))
            if r.content_days <= 0 or r.metadata_days <= 0:
                raise ValueError(f"{tenant}: retention days must be positive")
            if r.content_days > r.metadata_days:
                raise ValueError(f"{tenant}: content cannot outlive the metadata that indexes it")
            out[str(tenant)] = r
        return RetentionPolicy(out)

    def for_tenant(self, tenant: str) -> Retention:
        return self.tenants.get(tenant, self.tenants["*"])


@dataclass(frozen=True)
class AuditRecord:
    call_id: str
    response_id: str | None
    trace_id: str | None
    event_time: datetime
    end_time: datetime | None
    status: str
    call_type: str
    model: str
    model_group: str | None
    tenant: str
    team_id: str | None
    key_alias: str
    key_hash: str | None
    end_user: str | None
    end_user_verified: bool
    requester_ip: str | None
    prompt_tokens: int
    completion_tokens: int
    messages: object
    response: object
    error: str | None
    redactions: dict[str, int]
    flags: list[str] = field(default_factory=list)
    content_sha256: str = ""
    retain_until: datetime | None = None  # metadata (audit.events)
    content_retain_until: datetime | None = None  # prompt and answer (audit.event_content)


def parse_body(raw: bytes) -> list[dict]:
    """A JSON array, a single JSON object, or newline-delimited JSON objects."""
    text = raw.decode("utf-8").strip()
    if not text:
        raise PayloadError("empty body")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        try:
            doc = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as err:
            raise PayloadError(f"invalid JSON: {err.msg}") from None
    items = doc if isinstance(doc, list) else [doc]
    if not all(isinstance(i, dict) for i in items):
        raise PayloadError("every event must be a JSON object")
    return items


def _time(value: object) -> datetime | None:
    """StandardLoggingPayload uses epoch seconds; the legacy v1 payload uses strings."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=UTC)
    s = str(value)
    try:
        return datetime.fromtimestamp(float(s), tz=UTC)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _str(value: object) -> str | None:
    return None if value is None or value == "" else str(value)


def _int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def _ip(value: object) -> str | None:
    try:
        return str(ipaddress.ip_address(str(value))) if value else None
    except ValueError:
        return None


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def to_record(
    payload: dict,
    *,
    redactor: Redactor,
    retention: RetentionPolicy,
    trusted_user_keys: frozenset[str],
    now: datetime | None = None,
) -> AuditRecord:
    """Build one redacted record. Raises PayloadError only for an unusable event."""
    meta = payload.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    now = now or datetime.now(UTC)

    # Idempotency key: litellm_call_id is a UUID per call; "id" is the
    # provider's response id (and gets a suffix on cache hits). Fall back to a
    # content hash so an event with neither is still deduplicated on resend.
    call_id = _str(payload.get("litellm_call_id")) or _str(payload.get("id"))
    if call_id is None:
        call_id = "sha256:" + hashlib.sha256(_canonical(payload).encode()).hexdigest()

    model = _str(payload.get("model")) or _str(payload.get("model_group"))
    if model is None:
        raise PayloadError(f"event {call_id} has no model")

    key_alias = _str(meta.get("user_api_key_alias")) or "unknown-key"
    tenant = (
        _str(meta.get("user_api_key_team_alias"))
        or _str(meta.get("team_alias"))
        or _str(meta.get("user_api_key_team_id"))
        or "unassigned"
    )
    # The claimed person. Only chat-UI keys may vouch for it (Chapter 6).
    end_user = _str(payload.get("end_user")) or _str(meta.get("user_api_key_end_user_id"))

    counts: Counter[str] = Counter()
    raw_messages = payload.get("messages")
    raw_response = payload.get("response")
    flags = injection_signals(strings_in(raw_messages))
    messages = redactor.value(raw_messages, counts)
    response = redactor.value(raw_response, counts)
    err = payload.get("error_str")
    error = redactor.text(str(err), counts) if err else None

    event_time = _time(payload.get("startTime")) or now
    keep = retention.for_tenant(tenant)
    status = "failure" if payload.get("status") == "failure" else "success"

    return AuditRecord(
        call_id=call_id,
        response_id=_str(payload.get("id")),
        trace_id=_str(payload.get("trace_id")),
        event_time=event_time,
        end_time=_time(payload.get("endTime")),
        status=status,
        call_type=_str(payload.get("call_type")) or "unknown",
        model=model,
        model_group=_str(payload.get("model_group")),
        tenant=tenant,
        team_id=_str(meta.get("user_api_key_team_id")) or _str(meta.get("team_id")),
        key_alias=key_alias,
        key_hash=_str(meta.get("user_api_key_hash")),
        end_user=end_user,
        end_user_verified=end_user is not None and key_alias in trusted_user_keys,
        requester_ip=_ip(payload.get("requester_ip_address") or meta.get("requester_ip_address")),
        prompt_tokens=_int(payload.get("prompt_tokens")),
        completion_tokens=_int(payload.get("completion_tokens")),
        messages=messages,
        response=response,
        error=error,
        redactions=dict(sorted(counts.items())),
        flags=flags,
        content_sha256=hashlib.sha256(_canonical([messages, response, error]).encode()).hexdigest(),
        retain_until=event_time + timedelta(days=keep.metadata_days),
        content_retain_until=event_time + timedelta(days=keep.content_days),
    )
