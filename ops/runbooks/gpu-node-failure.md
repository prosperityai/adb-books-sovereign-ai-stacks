# Runbook: GPU Node Failure

Book: Sovereign AI Stacks, Chapter 12. Owner: platform on-call. Severity: P1 if a model alias has no running replica, P2 if capacity is reduced but every alias still answers.

## Symptoms

- Alert `KubeNodeNotReady` for `gpu-node-1` or `gpu-node-2`, or DCGM metrics for a node stop arriving (Chapter 9).
- `Xid` errors in the node's kernel log, or `nvidia.com/gpu` allocatable drops below 8.
- vLLM pods `Pending` with `0/5 nodes are available: ... Insufficient nvidia.com/gpu`.
- Gateway logs show `qwen3.8-27b` requests answered by `gpt-oss-120b` (fallback) or failing with 429/503.

## 1. Triage (5 minutes)

```console
$ kubectl get nodes -l nvidia.com/gpu.present=true \
    -o custom-columns='NODE:.metadata.name,READY:.status.conditions[?(@.type=="Ready")].status,GPUS:.status.allocatable.nvidia\.com/gpu'
$ kubectl -n inference get pods -o wide
$ talosctl -n <gpu-node-ip> dmesg | grep -iE 'xid|nvrm|fallen off the bus' | tail -20
```

Decide which case you're in:

| Finding | Case |
|---|---|
| Node `NotReady`, no Talos API response | A: node down |
| Node `Ready`, fewer GPUs allocatable, `Xid 79` or "fallen off the bus" | B: one GPU lost |
| Node `Ready`, all GPUs allocatable, vLLM crash-looping | Not hardware. Check the pod log for CUDA OOM, then `model-rollback.md` |

## 2. Keep the service up

Model servers use `strategy: Recreate` and a ReadWriteOnce cache volume (Chapter 5), so a replacement pod needs a free GPU on a healthy node **and** the volume detached from the dead node.

1. Cordon the failed node so nothing new lands there:
   `kubectl cordon <node>`
2. Case A only: once you're sure the node is really off (check the BMC console, and power it off there if it's hung), release its volumes. Kubernetes won't detach a volume from a node it can't reach:
   `kubectl taint nodes <node> node.kubernetes.io/out-of-service=nodeshutdown:NoExecute`
3. Check where the pods went. With 4 spare GPUs on the surviving node (Chapter 5), the evicted replicas reschedule within minutes, and most of that time is spent loading weights from Ceph.
4. If they can't fit, protect the interactive alias first. Scale down the batch-heavy deployment and tell the batch tenant:
   `kubectl -n inference scale deploy/vllm-gpt-oss-120b --replicas=0`
   `qwen3.8-27b` keeps serving, and `gpt-oss-120b` requests fail fast with a clear error instead of queueing.
5. Announce reduced capacity to tenant focal points, with an estimated time for the fix.

## 3. Repair

- **Case B (one GPU)**: drain the node (`kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`), then reboot it with `talosctl -n <ip> reboot`. If the GPU is still missing after the reboot, it's a hardware ticket. Raise it with the vendor, quoting the Xid code and the GPU serial from `talosctl -n <ip> dmesg`.
- **Case A (node down)**: work through the BMC: power cycle and watch the console. If the system disk is gone, reinstall from the GPU installer image with the node's original machine config (`infra/talos/out/worker.yaml`):
  `talosctl apply-config --insecure --nodes <ip> --file infra/talos/out/worker.yaml`
  The node rejoins with the same labels. `bootstrap.sh` step 7 adds the taint again: `kubectl taint nodes <node> nvidia.com/gpu=present:NoSchedule --overwrite`.

## 4. Return to service

```console
$ kubectl taint nodes <node> node.kubernetes.io/out-of-service- || true
$ kubectl uncordon <node>
$ kubectl get node <node> -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'   # expect 8
$ kubectl -n inference scale deploy/vllm-gpt-oss-120b --replicas=1                # if scaled down in step 2
```

Confirm the GPUs are healthy before you trust the node with traffic: no new Xid errors for 30 minutes, and DCGM's `DCGM_FI_DEV_XID_ERRORS` is at 0 on the dashboard.

## 5. Record

Write down the times, the Xid codes, the GPU serials, whether any alias was unavailable (and for how long), and the vendor ticket number. Two failures of the same card in 90 days is grounds for an RMA, so this record is your evidence.
