# Community Sentiment Design

Date: 2026-08-16  
Project: daily_stock_analysis  
Status: Approved for spec; implementation plan not started

## Problem

Community context for the LLM comes from Xueqiu only (`XueqiuSentimentService` → `https://xueqiu.com/query/v1/search/status.json`). Direct requests return HTTP 200 with Aliyun WAF HTML, not JSON. Production has `XUEQIU_SENTIMENT_ENABLED=true` and no `XUEQIU_COOKIE`. The service treats non-JSON 200 as an empty list, so the prompt looks like “no discussion” instead of “blocked.” RSSHub Xueqiu routes also fail (500/503); the RSSHub **news** block is separate and stays as-is.

User-facing impact: individual-stock analysis has no real community posts. News and finance intel are out of this change except that they must not be duplicated into the community block.

## Goals

- Prefer Xueqiu when a cookie is configured and the search API returns parseable posts.
- If Xueqiu is skipped or fails, fill the same community block from Eastmoney guba/comments (AkShare), not from RSSHub news.
- Label the source in the prompt (Xueqiu vs Eastmoney).
- Treat WAF / non-JSON as interception, not as empty discussion.
- Do not use Browserless or any headless browser.

## Non-goals

- Repairing RSSHub `/xueqiu/*` routes
- Search-engine API keys (Tavily / Bocha / Brave / SerpAPI)
- Chip distribution, K-line fallbacks, finance intel
- Parallel fetch of Xueqiu and Eastmoney on every query
- Storing or logging cookie values

## Decisions

| Topic | Choice |
|------|--------|
| Xueqiu effort | Direct HTTP + optional `XUEQIU_COOKIE` only |
| When cookie is missing | Do not call Xueqiu; go to Eastmoney |
| On Xueqiu success with posts | Use Xueqiu only; skip Eastmoney |
| On Xueqiu fail / empty | Eastmoney guba or comments |
| News block | Unchanged |

## Architecture

`pipeline._build_intel_context` still calls one `build_sentiment_context(code, name)` then RSSHub news. Branching stays inside the sentiment service.

Three units in `src/services/` (same file or small helpers; pipeline does not contain fetch policy):

1. **Orchestrator** — today’s `XueqiuSentimentService`, renamed to `CommunitySentimentService`. Same public method. Adds a `source` field on the result. Pipeline only changes import/attribute names if the class is renamed.

2. **Xueqiu adapter** — cookie gate, home warmup + search JSON, WAF/non-JSON detection, in-process skip after WAF, post text/author parse. No AkShare.

3. **Eastmoney adapter** — per-stock AkShare guba or comment API (pick the first interface that returns post text; guba list first, comment APIs if needed). Returns texts or an error string. Does not raise to the orchestrator.

`SentimentResult` keeps `sample_count`, `highlights`, `kol_highlights`, `error`, and adds `source`: `xueqiu` | `eastmoney` | `none`.

## Data flow

Cookie gating always applies. The fallback switch only controls whether Eastmoney runs.

For each stock:

1. If `XUEQIU_SENTIMENT_ENABLED` is false: skip Xueqiu. If fallback is also false, return an empty string (same as today’s disabled service). If fallback is on, go to step 4.
2. If `XUEQIU_COOKIE` is empty: INFO log that Xueqiu is skipped; go to step 4 (or, if fallback is off, emit a community block with count 0 and reason “no cookie,” not “no discussion”).
3. If cookie is set but the process-level Xueqiu skip flag is set: do not call Xueqiu; go to step 4 if fallback is on. If cookie is set and the flag is unset: GET `https://xueqiu.com/` (about 8s timeout) then GET `search/status.json` (about 10s) with `q="{name} {code}"`.
   - JSON with at least one extracted text → community block source Xueqiu; stop.
   - DNS / timeout / connection error, non-200, `Content-Type` not JSON, body contains Aliyun WAF markers, or JSON list empty → WARNING with a reason class (`blocked` / `empty` / `network`); go to step 4 if fallback is on, otherwise emit count 0 with that reason.
   - If reason is `blocked` (WAF or non-JSON): set a process-level flag so later stocks in this process skip Xueqiu.
4. If fallback is off, stop after the Xueqiu attempt (or skip). If fallback is on: Eastmoney adapter with timeout. Cap raw items with existing `XUEQIU_SENTIMENT_MAX_POSTS`; still show at most 5 highlights.
   - Success → block source Eastmoney; if Xueqiu was skipped or failed, one line: Xueqiu unavailable, using Eastmoney.
   - Failure → still emit the community heading; sample count 0; include Xueqiu reason (if any) and Eastmoney reason. Do not raise.
5. KOL matching (`XUEQIU_KOL_USERS`) applies only to Xueqiu authors. Eastmoney blocks omit KOL hit lines.

## Error handling and logging

- Orchestrator never raises into the pipeline (pipeline already wraps this call).
- Do not log cookie contents.
- WAF HTML on HTTP 200 is `blocked`, not “no valid discussion.”
- Eastmoney exceptions become `error` on the result.
- Timeouts: Xueqiu home ~8s, search ~10s; Eastmoney similarly bounded so a hung AkShare call cannot stall the whole analysis.

## Configuration

Reuse `XUEQIU_SENTIMENT_ENABLED`, `XUEQIU_COOKIE`, `XUEQIU_USER_AGENT`, `XUEQIU_SENTIMENT_MAX_POSTS`, `XUEQIU_KOL_USERS`.

Add `COMMUNITY_SENTIMENT_FALLBACK_ENABLED` (default `true`). No new required secrets. Empty cookie plus fallback on is the production path.

If `XUEQIU_SENTIMENT_ENABLED=false`, skip Xueqiu even when a cookie exists; still run Eastmoney when fallback is on.

## Prompt shape

Keep the `### 💬 社区舆情` heading. Always include:

- Source line: Xueqiu or Eastmoney (or none)
- Sample count
- Up to 5 highlight lines, or an explicit failure/empty explanation

Do not imply the market has no discussion when the cause is WAF or a missing cookie.

## Testing

No live Xueqiu or Eastmoney network in unit tests. Fake HTTP and fake AkShare.

1. No cookie → Xueqiu HTTP is not called; Eastmoney highlights appear with Eastmoney source.
2. Cookie + JSON posts → Xueqiu only; Eastmoney is not called.
3. Cookie + WAF HTML 200 → fallback to Eastmoney; second call in the same process does not hit Xueqiu.
4. Xueqiu empty list and Eastmoney empty → block still rendered, count 0, both reasons present.
5. Eastmoney raises → no exception to caller; block contains the failure reason.

Acceptance on production without cookie: a stock query’s community block shows Eastmoney samples **or** an explicit dual-failure message, not the current “sample count 0 / no valid discussion” wording caused by WAF HTML.

## Rollback

Set `COMMUNITY_SENTIMENT_FALLBACK_ENABLED=false` to stop Eastmoney. Unset `XUEQIU_COOKIE` to stop Xueqiu requests. Revert the service/pipeline import if a full code rollback is needed.
