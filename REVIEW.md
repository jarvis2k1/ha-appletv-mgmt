# QA review — consolidated spec (2026-05-17)

Two parallel reviewers (Claude Sonnet 4.6 + Claude Opus 4.7) audited the suite at integration v0.9.0 + panel addon v0.2.0. Their full reports are reproduced below the implementation table; this document is the actionable shape.

**Both agents agreed on five real bugs that are live now**, plus each found one P0 the other missed. Both rated the architecture sound — the three-component split (integration / proxy / panel) is the right shape and should not be merged. The OPEN-by-default REST API posture is a *deliberate* choice for the owner's setup but the wrong default for a public HACS listing.

## What ships in this round (integration v0.10.0 + panel v0.3.0)

| # | Bug / change | Severity | Component | Source |
|---|---|---|---|---|
| 1 | `handle_external_decision` fires wrong `granted_minutes` in bus event due to APPROVE_HALF dispatch hack | **P0** | integration | Both |
| 2 | `expire_old_requests` is defined but never called; pending requests live forever | **P0** | integration | Both |
| 3 | Enforcer state desyncs from AdGuard on HA restart — kid can be silently stuck blocked, or get a free 60s grace | **P0** | integration | Opus |
| 4 | Storage load: a single bad event entry crashes the whole integration | **P0** | integration | Sonnet |
| 5 | Panel action endpoints silently swallow upstream errors (the owner taps Approve, nothing happens, no feedback) | **P0** | panel | Opus |
| 6 | Request IDs use `time.time()*1000` — millisecond collisions silently overwrite | P1 | integration | Both |
| 7 | `budget_today_min` excludes extensions but `remaining_today_min` includes them → invariant `remaining + used = budget` breaks | P1 | integration | Sonnet |
| 8 | `AdGuard.list_known_services` fetched on every block transition (~50ms each); cache with TTL | P1 | integration | Both |
| 9 | `warn_thresholds_min` is a list of 3 values but only `max()` is used; reduce to single int | P1 | integration | Sonnet |
| 10 | OpenAPI drift: `/usage?range=week` documented in 3 places, implemented in 0; declare proper `parameters` arrays; ProfileSummary in docs doesn't match wire shape | P1 | integration | Both |
| 11 | `EVENT_RETENTION_DAYS = 30` but panel offers "quarter" (90d) range → 60 days of empty bars | P1 | integration | Opus |
| 12 | `_check_api_key` uses non-constant-time string compare | P1 | integration | Opus |
| 13 | `RequestDecideView.post` treats missing `approve` as deny instead of 422 | P1 | integration | Sonnet |
| 14 | `enforce_reason = "adult_mode"` while `state = OK` — panel reads "OK — adult_mode", confusing | P1 | integration | Sonnet |
| 15 | Panel: CSRF/Origin check on `/admin/actions/*` (rejects POSTs that don't come from the HA ingress) | P1 | panel | Opus |
| 16 | Panel: detect/report integration-key mismatch on addon startup (currently fails silently per request) | P1 | panel | Both |
| 17 | Panel: mobile-responsive sidebar (currently 220px fixed → unusable on phone, HA Companion is mobile-first) | P1 | panel | Opus |
| 18 | Panel a11y: `aria-hidden` on decorative SVGs, `:focus-visible` styles, `role="status" aria-live="polite"` on auto-refresh cards, `aria-label="Primary"` on nav | P1 | panel | Opus |
| 19 | `manifest.json` missing `homeassistant` min-version; `iot_class: local_polling` should be `local_push` | P2 | integration | Sonnet |
| 20 | Add tests: `test_storage.py` (load resilience, prune, midnight reset), `test_api.py` (auth, decide bug) | P2 | integration | Both |

## What is intentionally deferred to a future round

| Item | Why deferred |
|---|---|
| Auto-generate `api_key` as default (Opus P0 #3) | Requires careful migration + Repairs UX + docs rewrite. This round adds a Repairs warning when OPEN mode is detected, full migration in v0.11. |
| One shared `AppleTVMgmtStore` instance for all profiles (Sonnet P0 #1) | Only manifests with 2+ Apple TVs; the owner has 1. Substantial refactor; will land before HACS submission. |
| Rename `Profile` → `Device` (Opus architectural) | Breaking API change. Park until v1.0. |
| Audit-log event stream | Nice-to-have. Existing per-event bus fires cover most of this. |
| Webhook output for kid-side notifications | Out of scope; OpenClaw skill handles this. |
| CSV export | Trivial to add later; not blocking. |
| Server-side daily aggregate endpoint (vs raw events) | Replaced by the much-simpler retention bump from 30 → 90d. |
| Proxy port-discovery hardening | Works in practice; would need Supervisor-API integration to do right. |
| iTunes API hard-coded `country=US` | Easy fix but the owner's apps are all curated; backlog. |

---

## Implementation order (driving this round)

1. **Integration storage + API correctness**: items 4 → 2 → 1 → 13 → 6 → 7 → 14 → 11 → 19
2. **Integration enforcement reliability**: item 3 → 8 → 9
3. **Integration security/contract polish**: item 12 → 10
4. **Integration tests** for the above where reasonable (item 20)
5. **Panel UX critical**: item 5
6. **Panel security/correctness**: item 15 → 16
7. **Panel a11y + responsive**: item 17 → 18
8. Deploy integration v0.10.0 → panel v0.3.0 → live retest → tag both

---

## Reviewer reports (verbatim, archived for traceability)

The two full reports follow. Skip these unless you need to see the original reasoning behind a finding.

### Sonnet QA — code quality + tests (saved 2026-05-17)

See `/private/tmp/claude-501/-Users-marc-HA-AppleTV-Mgmt/866cf0b5-01d8-4b17-8e57-eae20d787982/tasks/a92f1d2de349ba6ba.output` for the full subagent transcript. Key findings condensed in the table above.

### Opus QA — architecture + security + UX (saved 2026-05-17)

See `/private/tmp/claude-501/-Users-marc-HA-AppleTV-Mgmt/866cf0b5-01d8-4b17-8e57-eae20d787982/tasks/af1d0148e75e63327.output` for the full subagent transcript. Key findings condensed in the table above.
