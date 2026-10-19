# Runbook: Model Rollback

Book: Sovereign AI Stacks, Chapter 12. Owner: platform on-call. Severity: P2 if answers are wrong or slow for one tenant, P1 if every tenant is affected.

## When to use this

- `upgrade_model.sh analyse` returned `rollback` (exit code 1).
- Users or the tenant's focal point report worse answers since a model change, in any language. Swahili and Arabic regressions are the ones English-speaking engineers miss.
- vLLM pods for a newly promoted model restart, or TTFT p95 on the Chapter 9 dashboard is above 3 s.

Do **not** use this for a GPU or node fault. That's `gpu-node-failure.md`.

## 1. Find out where the upgrade is (2 minutes)

```console
$ ops/scripts/upgrade_model.sh status qwen3-8-27b
```

| `status` shows | State | Go to |
|---|---|---|
| `canary vllm-qwen3-8-27b-canary ...` | Staged or canary, not promoted | Step 2 |
| `canary none` and a `previous` reference | Promoted | Step 3 |
| `canary none` and no `previous` | Nothing to roll back | Escalate: the change didn't go through `upgrade_model.sh` |

## 2. Roll back a canary (under 5 minutes, no user impact)

```console
$ ops/scripts/upgrade_model.sh rollback qwen3-8-27b
```

This puts the gateway back on the reviewed `litellm-values.yaml`, removing both the weighted canary and the `qwen3.8-27b-candidate` alias, then deletes the canary Deployment, Service and cache volume. The stable model never changed, so there's nothing else to verify beyond step 4.

## 3. Roll back a promotion (15–40 minutes)

```console
$ ops/scripts/upgrade_model.sh rollback qwen3-8-27b
```

- **With spare GPUs** (the normal case, 4 free on the reference node): the script stages the previous digest as a new canary, skips the eval gate because that model already passed it, and promotes it. Users see no outage. Most of the time goes on pulling about 31 GB from Harbor into a fresh cache volume.
- **Without spare GPUs**: the script restarts the stable Deployment on the previous digest in place. While it restarts, `qwen3.8-27b` requests fall back to `gpt-oss-120b` (the Chapter 6 `fallbacks` rule). Tell the tenants' focal points before you start: answers will be slower and styled differently for up to 20 minutes.

## 4. Verify

```console
$ ops/scripts/upgrade_model.sh status qwen3-8-27b
$ kubectl -n inference logs deploy/vllm-qwen3-8-27b -c model-fetch | tail -2
```

- The stable reference is the digest you expected, and the init container logged `verified ... layer digests`.
- On the Chapter 9 dashboard, TTFT p95 and request rate for `qwen3.8-27b` are back at their pre-upgrade levels within 15 minutes.
- Re-run Chapter 11's acceptance Job with the restored model as candidate if the trigger was answer quality. The rollback isn't finished until the gate passes again.

## 5. Make Git match the cluster

The rollback changed `MODEL_REF` live. Set it in `platform/inference/vllm-qwen3-8-27b.yaml` to the digest `status` shows, and merge that change the same day. Re-applying a stale manifest is how a rolled-back model comes back.

## 6. Record

Open an incident record with: the digest rolled back from and to, the trigger (gate verdict JSON, user report or alert), start and end times, and whether users saw the `gpt-oss-120b` fallback. Attach it to the evidence pack (Chapter 10).
