---
name: manage-crawler-ip-pool-gateway
description: Operate, inspect, test, and maintain the standalone fixed-port multi-egress crawler gateway backed by Mihomo/Xray, including multi-target website profiles, remotely refreshed Shadowrocket subscriptions, reserve-pool qualification, primary work-lane assignment, subscription-first routing, optional observed-IP-aware spreading, isolated failover, factual runtime reporting, crawler integrations, and clean shutdown. Use when Codex needs to run or troubleshoot Managed-Crawler-IP-Pool-Gateway, maintain one or many target-specific proxy pools, connect a crawler to fixed local proxy lanes, resume the optional NSFC integration, or adapt the gateway to another target website.
---

# Manage Crawler IP Pool Gateway

This is the only active gateway-management Skill. Do not use the retired `route-nsfc-through-shadowrocket` workflow as an alternative product.

Resolve the standalone gateway root in this order: `$CRAWLER_GATEWAY_ROOT`, the repository containing this Skill (`../../` from this file), then `$HOME/Documents/codex/managed_crawler_ip_pool_gateway`. Never treat a consumer crawler's copied `gateway/` directory as canonical. Keep Shadowrocket as the Codex/OpenAI control plane; do not switch its main node merely to operate this gateway.

## Core Workflow

1. Read `README.md` and `private/gateway.yaml`, but never print or copy subscription URLs, UUIDs, secrets, or full node configs into chat or Git.
2. Run `pool-status --plain` and the regular status command first. Distinguish raw reserve inventory, fresh target-qualified reserve nodes, and primary work-lane leases.
3. Treat every configured target independently. A node qualified for one website is not implicitly qualified for another.
4. Start the gateway. Use `refresh-reserve` to update providers without probing, `probe-reserve` to test one or many targets without refreshing, or `maintain-reserve` to compose both on a schedule. For unattended macOS operation, use `install-service` and verify both the LaunchAgent and maintenance PID.
5. For Shadowrocket subscription providers with `refresh_remote: true`, fetch the hidden subscription records without printing their URLs. Cache the source URL manifest with mode `600` under ignored runtime storage so the LaunchAgent does not repeatedly open the Shadowrocket archive. Keep an independent hashed node cache for every source so one failed source does not block fresh updates from the others. A foreground `refresh-reserve` intentionally refreshes the manifest after the user adds or removes an entire subscription record.
6. Assign primary work lanes only from the fresh target-qualified reserve subset. Public-IP detection is optional metadata.
7. Before starting any consumer crawler, require the requested number of lanes verified for that crawler's target profile. For NSFC, also check for an existing writer.
8. Monitor the crawler and its assigned target lanes. For NSFC, also monitor detail errors and the strict next checkpoint. Never leave required command sessions running when ending the task.

## Commands

Use `/opt/anaconda3/bin/python3` and run from the project root.

```bash
/opt/anaconda3/bin/python3 -m crawler_gateway --config private/gateway.yaml status --plain
/opt/anaconda3/bin/python3 -m crawler_gateway --human --config private/gateway.yaml start
/opt/anaconda3/bin/python3 -m crawler_gateway --human --config private/gateway.yaml refresh-reserve
/opt/anaconda3/bin/python3 -m crawler_gateway --human --config private/gateway.yaml probe-reserve TARGET_NAME
/opt/anaconda3/bin/python3 -m crawler_gateway --config private/gateway.yaml pool-status --plain
/opt/anaconda3/bin/python3 -m crawler_gateway --human --config private/gateway.yaml assign TARGET_NAME --lanes 3
/opt/anaconda3/bin/python3 -m crawler_gateway --human --config private/gateway.yaml monitor TARGET_NAME
/opt/anaconda3/bin/python3 -m crawler_gateway --human --config private/gateway.yaml maintain-reserve --once
/opt/anaconda3/bin/python3 -m crawler_gateway --config private/gateway.yaml install-service
/opt/anaconda3/bin/python3 -m crawler_gateway --config private/gateway.yaml service-status
/opt/anaconda3/bin/python3 -m crawler_gateway --config private/gateway.yaml stop-maintenance
/opt/anaconda3/bin/python3 -m crawler_gateway --config private/gateway.yaml disable-service
/opt/anaconda3/bin/python3 -m crawler_gateway --config private/gateway.yaml stop
```

The optional NSFC adapter adds `run-nsfc` and `stop-nsfc`; use them only when `NSFC_REPO` and `NSFC_DATA_DIR` point to the separate NSFC project.

For the user's manual workflow, the equivalent double-click files are under `commands/`.

## Routing Rules

- Model three explicit stages: all discovered provider nodes, per-target fresh qualified reserve nodes, and primary fixed-port leases.
- Keep failed/stale probe facts for statistics and later retesting, but exclude them from target-qualified reserve candidates.
- Rank candidates in this order: Shadowrocket subscription nodes, Shadowrocket local/manual nodes, then `[local] DIRECT` as the final fallback.
- Let subscription nodes follow `node_outbound_interface`; it is intentionally empty by default.
- Keep one crawler worker pinned to one fixed local port for its lifetime.
- Keep every unassigned work/probe selector on `REJECT`, not `DIRECT`. On a fresh Mihomo start, fail closed first and restore only leases that still exist in SQLite.
- Verify `group.now` still equals the leased proxy name before treating a lane probe as healthy. A successful target response through an unexpected selector is a binding failure.
- Use separate temporary probe ports for inventory and release every probe lane afterward.
- Prefer distinct known egress IPs within each source tier, but do not reject target-healthy nodes merely because their public IP is unknown or duplicates another node.
- Preserve cached provider files when a subscription refresh temporarily fails. For multi-source Shadowrocket subscriptions, merge fresh sources with only the corresponding failed source caches and report `success`, `partial`, or `cache` factually.
- Print all non-sensitive operational parameters and factual pool results. Never print provider URLs or paths, header values, body values, UUIDs, or full node definitions.
- Treat `gateway.work_lanes` as configurable capacity. Keep 3 as the conservative default, prefer 3-5 on one personal machine, and retain the default `max_work_lanes: 6` guard unless deliberate testing supports more.
- Keep every work lane pinned to its own fixed port, selector, failure counter, and crawler worker. Never reassign a healthy lane merely because another lane fails.

## Failure Policy

- Rotate only the failed lane for proxy transport failures, TLS/connectivity failures, an invalid target response, or a dead lane. Missing egress IP alone is not a failure.
- Probe lanes independently. One lane's exception must not abort checking, monitoring, or failover for the others.
- Report a suspected shared target outage only when matching explicit failures come from at least two known distinct egress IPs. Same-IP or unknown-IP failures may be a shared IP block and must not suppress failover. After a distinct-IP shared failure reaches the configured threshold, rotate only one canary lane so an outage inference cannot stall forever.
- At the start of every reserve-maintenance cycle, verify the backend process and control API. Rebuild from safe provider caches and restart Mihomo when needed; use `maintenance_error_retry_seconds` after failed cycles.
- When one lane has no verified replacement, preserve its lease as degraded and retry later. Do not clear, release, reassign, or restart healthy lanes.
- Remember the shared-core boundary: a Mihomo process failure affects all lane ports even though node and route failures are isolated.
- Write the configured fault file with current factual progress on operational failure; clear it only after a real crawler start succeeds.
- For integrations that write a shared dataset, refuse duplicate writers to the same output directory.

## Data Integrity

- Keep the NSFC base SQLite read-only.
- Resume from the first sorted `record_id` missing a valid detail JSON.
- Preserve existing successful files and archive only transient detail errors using the established NSFC autopilot behavior.
- Treat validation errors as a hard stop.
- Do not claim the directory's total JSON count is continuous progress when later records were written out of order.
- Update inventory qualification incrementally. Keep unchanged nodes' last fresh state during a long scan, and invalidate only newly discovered or stable-fingerprint-changed nodes.

## Background Service

- The macOS LaunchAgent label is `com.yangyuezh.crawler-gateway.reserve-maintenance`.
- Its plist contains only executable/config paths and operational arguments, never subscription URLs or node credentials.
- Avoid using a protected `Documents` directory as the LaunchAgent working directory. Use `/` plus a read-only `PYTHONPATH` to the project.
- Verify `service-status`, `runtime/reserve_maintenance.pid`, and `logs/reserve-maintenance.jsonl`; a merely loaded `xpcproxy` is not sufficient proof that the maintenance loop started.
- The rotating JSONL event log is capped at 10 MB with five backups. Startup errors go to `logs/reserve-maintenance.err.log`. Keep probe history for the configured retention window (90 days by default) while preserving current node-target snapshots.

## Verification

After code changes, run:

```bash
/opt/anaconda3/bin/python3 -m pytest -q
/opt/anaconda3/bin/python3 -m compileall -q crawler_gateway tests
/opt/anaconda3/bin/python3 -m crawler_gateway --config private/gateway.yaml validate
```

For a real smoke test, inventory one or two candidates and then verify no listeners remain on `17991-17992`. Do not start the production crawler merely to test gateway code.

Before Git operations, confirm `private/`, `runtime/`, and `logs/` are ignored and search tracked files for subscription URLs, credentials, and UUIDs.
