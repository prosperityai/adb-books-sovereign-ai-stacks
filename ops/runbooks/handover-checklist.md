# Handover Checklist

Book: Sovereign AI Stacks, Chapter 12.

The platform is handed over when the client's team has **done** every item below themselves, on their own platform, with the integrator watching and not touching the keyboard. Each line needs the client engineer's initials, the date, and where the evidence is kept. A line someone has only read about doesn't count.

## People and on-call

- [ ] Named owners for platform, models, identity and backups, each with a named deputy. No single person holds a role alone.
- [ ] On-call rota published for the next 8 weeks: primary and secondary, phone numbers, escalation to the vendor contract.
- [ ] Severity definitions agreed with the tenants' focal points (P1: an alias or sign-in is down for everyone; P2: degraded or one tenant; P3: no user impact).
- [ ] Integrator remains as third-line support only, for the period the contract specifies, and is reachable through the client's ticket system, not personal phones.

## Keys and secrets (custody)

- [ ] Talos `secrets.yaml`, `talosconfig` and the admin kubeconfig are held in the client's secret store. Two people know how to retrieve them.
- [ ] cosign signing key (Chapter 3): the key ceremony was re-run with client custodians, and the integrator's copy is destroyed (record the witness).
- [ ] Backup appliance credentials: separate keys for Velero, CloudNativePG (per namespace) and Qdrant, each limited to its bucket. Root credentials for the appliance are held by the client only.
- [ ] Velero Kopia repository password (`velero-repo-credentials`) and the CloudNativePG/Qdrant S3 keys are escrowed **off the cluster**, in the sealed envelope or HSM the client uses for break-glass material. Without them, the backups can't be restored on a rebuilt cluster.
- [ ] LiteLLM master key rotated by the client. The integrator's admin access is revoked in Keycloak and on the gateway.

## Supply chain

- [ ] Client engineer built, signed, carried and imported a bundle end to end (Chapter 3): `verify_bundle.sh` → Harbor, with their own hands on the transfer media.
- [ ] Every image running in the cluster resolves in Harbor (the Chapter 4 `running-images.txt` check), and the diff is empty.

## Upgrades

- [ ] Model upgrade done by the client: `upgrade_model.sh stage` → Chapter 11's acceptance run against `qwen3.8-27b-candidate` passed → `canary 10` → `analyse` → `promote`, followed by the matching Git change.
- [ ] Model rollback done by the client, both from a canary and after a promotion (`ops/runbooks/model-rollback.md`).
- [ ] Talos and Kubernetes patch upgrade rehearsed on the lab cluster with `upgrade_cluster.sh`, including a preflight that **failed** because an image was missing from Harbor.
- [ ] Barman plugin certificate expiry date entered in the operations calendar, with a reminder 30 days before (`barman_plugin_tls.sh` prints the date). Rotation rehearsed once.

## Backups

- [ ] `kubectl -n velero get backupstoragelocation` shows `enclave-backup` `Available`. The appliance is on separate hardware, in a different room, on different power from the cluster.
- [ ] Last successful backup for each source is less than 26 hours old: Velero `platform-resources` and `tenant-data`, CloudNativePG `litellm-db`, `keycloak-db`, `audit-db`, and the Qdrant `qdrant-snapshot` CronJob.
- [ ] Alerts `VeleroBackupStale` and `VeleroBackupFailed` fire on a test and reach the on-call phone, not just a dashboard.
- [ ] Object lock (WORM) is enabled on the appliance buckets, with a retention shorter than the backup retention so that pruning still works.

## Disaster recovery

- [ ] Client engineer ran `dr_drill.sh`, and `report.md` shows PASS against the RTO and RPO in the service agreement. The report is filed in the evidence pack.
- [ ] Full rebuild rehearsed at least once: a lab cluster bootstrapped from bare metal (Chapter 4), then restored from the appliance alone, with no access to the old cluster.
- [ ] The DR drill is in the calendar quarterly, with a named owner. Its reports go to the same people who receive the security evidence pack.

## Runbooks and knowledge

- [ ] Every runbook in `ops/runbooks/` was executed once by the client (in the lab where it's destructive), and corrections were merged by the client.
- [ ] `docs/threat-model.md` residual-risk owners updated to client staff.
- [ ] The repository, the Harbor admin account and the Keycloak `master` realm admin are owned by client accounts. Integrator accounts are disabled, not deleted, so the audit trail stays intact.

Signed for the client: ____________________  Date: __________

Signed for the integrator: ____________________  Date: __________
