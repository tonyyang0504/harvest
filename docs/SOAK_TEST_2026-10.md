# Soak test, 2026-10-01 to 2026-10-03

harvest ran unattended for about 47 hours on a shared Linux dev host. A driver injected one failure every 2 hours
for the first 24 hours. Two instances ran side by side: one on SQLite, one on Postgres. Every defect found was
fixed on branch `soak/fixes-20261001` with a regression test (`tests/test_soak_2026_10.py`), and the fixes were
then checked live against the same fault fixtures.

**Result in one paragraph.** The core promises held. In 2,038 runs, a failure never wiped data. A worker killed
with `kill -9` lost no job: its lease expired, the job ran again (attempts 2) and the half-done run was closed as
`stuck`. Restarting the web app never affected work in progress. Memory and file handles stayed flat. After
47 hours, the stored data of the fixture sources matched the served ground truth exactly. The weak spots were
operational:

- alerts were missed or never delivered;
- `degraded` alerts were false;
- a clock that ran ahead stalled scheduling;
- with clocks out of step, one job ran three times;
- a dead Postgres session was never re-opened;
- a truncated feed was recorded as a complete, empty walk;
- outages looked like empty sources;
- killed walks left scratch directories behind.

All of them are fixed.

## 1. Setup

| | |
|---|---|
| Host | dev, 8 cores, 23 GB RAM shared with many services (about 4.6 GB free), root disk 94–100 % full |
| Code under test | `main` at `2eb03e3` (package `harvest`, before the `harvest-ai` rename) |
| SQLite instance | `HARVEST_HOME` on a 256 MB tmpfs (isolated from the full root disk, and fillable on purpose) |
| Postgres instance | throw-away `postgres:16-alpine` container, data on a 256 MB tmpfs, 768 MB memory cap |
| Workers | `harvest worker --schedule 300` as transient systemd units, `MemoryMax=1G`, `HARVEST_WORKERS=2`, `Restart=always` |
| Web apps | `harvest web` on 127.0.0.1 only, one per instance |
| Alerts | `HARVEST_ALERT_WEBHOOK` pointed at a local sink that appended every POST to a file |
| Repair dispatch | off (`HARVEST_REPAIR_DISPATCH=off`) |

Two projects ran in each instance. Their sources were real, public and needed no key. They came from stub census
entries, so no agents were needed. Before any verdict was recorded, the terms and robots.txt of each site were
read.

| Project | Source | Lane | Cadence |
|---|---|---|---|
| soak-jobs (`jobs`) | hnrss.org/jobs (Hacker News job posts) | feed | 30 min |
| | Arbeitnow job-board API (capped at page 1) | JSON API | 1 h (its data changes hourly) |
| | fault fixture: RSS, a rotating 30-item window of a real We Work Remotely snapshot | feed | 15 min |
| soak-quakes (`events`) | USGS all-earthquakes-past-day GeoJSON | JSON API | 15 min |
| | GeoNet NZ quake API (CC BY 3.0 NZ) | JSON API | 30 min |
| | fault fixture: paginated HTML (3 × 15 items) of a real USGS snapshot | html | 15 min |

Four candidates were excluded:

- We Work Remotely: its terms page answers 403, so the terms could not be read.
- Himalayas and Jobicy: their terms forbid scraping.
- EMSC: robots.txt disallows it. harvest set it to lane `none` itself.

The fault fixture served ground truth. It logged the item ids of every response, and its mode could be switched
per resource: 500, 429, a well-framed truncated document, a short read, 0 items, a partial or full HTML redesign,
or slow pages. A sampler recorded memory, CPU, file handles, threads, cgroup process counts, DB sizes and table
counts, and leaked scratch directories every 10 minutes (287 samples).

## 2. Injections and verdicts

Each injection hit both instances unless marked otherwise. "FAIL" verdicts that came from the test harness and
not from harvest are listed in section 8.

| # | Injection | Expected | Observed (2026-10-01/02, UTC) |
|---|---|---|---|
| 1 | Jobs fixture answers 500 for 35 min | retries, no wipe, zero alert | 4 runs each, 3 retries per request; records 29 → 29; zero and stale alerts at the webhook; recovered with a replace of 29. Pass, **but** the runs were `empty`, not errors (D9). |
| 2 | Quakes fixture answers 429 (Retry-After 5) for 35 min | Retry-After honoured, no wipe, alert | `rate_limited` 4 and 3 retries per walk; records 45 → 45; zero alert delivered; recovered. Pass, with the same `empty` issue. |
| 3 | `kill -9` of each worker in the middle of a run job | lease recovery | Unit restarted in 6 s; the job was re-claimed after the 120 s lease and finished (attempts 2); the killed run was closed `stuck` about 10 min later and alerted; no orphan child process. **Scratch directory leaked** (D10). |
| 4 | Jobs RSS cut at 55 % (well-framed); quakes HTML short read (Content-Length lies) | no wipe, not complete, alert | No wipe in either case. The short read failed cleanly (`fetch_failed`, not complete). The **truncated RSS parsed as 0 items and the walk was marked complete** (D6). **No alert**: suppressed by the 24 h window of the alert in #1 (D2). |
| 5 | SQLite store tmpfs filled to 0 bytes free for 25 min | clean errors, recovery, no corruption | Inconclusive: SQLite kept writing into free pages of its WAL and database file. No error occurred, `integrity_check` was ok, and nothing was lost. The latent issue (an unwritable `alerts.jsonl` stops the webhook) was found by reading the code and fixed (D3). |
| 6 | Jobs fixture returns a valid feed with 0 items | runs `empty`, nothing deleted, zero alert | Runs `empty`, records 29 → 29. **No alert**: suppressed, because the same source had a zero alert 10 h earlier for a different incident (D2). |
| 7 | Quakes fixture: 80 % of the cards change markup, then all of them | `degraded`, then zero alerts; no repair dispatch | `degraded` runs (9 of 45 rows), no wipe, no repair dispatched. **The degraded alerts reached the table but never the webhook** (D1). The zero alert after the full redesign was suppressed (D2). |
| 8 | Web app restarted while a job it had queued was running | API back fast, job runs once | API back in 1.3–2.5 s with the same token; job done, attempts 1, one run. Pass. |
| 9 | Both workers 2 h ahead (libfaketime) for 25 min, then back to the real clock | the schedule continues | **No source ran from the return at 05:00 until 07:05** (the fixture's request log shows no request at all), and no stale alert (D4). |
| 10 | Postgres: every harvest session terminated; SQLite: `kill -9` during a scheduled tick | reconnect; the half-done run is closed | The **web app answered 500 ("the connection is closed") for that project until it was restarted a day later** (56 errors); the worker exited and systemd restarted it (D7, D8). The SQLite tick kill passed: run closed `stuck`, next tick ok. The stuck alert was suppressed (D2), and a scratch directory leaked (D10). |
| 11 | Both workers 2 h behind for 25 min; a run job submitted through the real-clock web app | the job runs once | SQLite: the web app's reaper saw the skewed lease as expired. The **job ran three times and ended `lost`** (D5). Postgres: the submit failed with 500 because of D7. The schedule continued after the return. |
| 12 | Postgres data tmpfs filled for 20 min | clean errors, recovery | Postgres stayed up. The worker had one crash and restart and recovered by itself. **The web app stayed at 500 after space was freed** (D7). No data loss. |

## 3. Runs and success rate

| Instance | Runs | ok | partial | degraded | empty | stuck |
|---|---|---|---|---|---|---|
| SQLite | 1,018 | 938 | 20 | 34 | 24 | 2 |
| Postgres | 1,020 | 938 | 20 | 33 | 28 | 1 |

On SQLite, `ok` + `partial` is 94.1 %. Every other run is explained:

- 25 came from injections: 20 `empty` fault runs, 3 `degraded` during the redesign and 2 `stuck` kills;
- 31 were false `degraded` runs of the capped Arbeitnow source (D11), which really stored a full page each;
- 4 `empty` Hacker News runs came from transient robots.txt failures upstream (`robots_denied` 1). harvest
  correctly obeyed them, but recorded them as `empty` (D9).

Counted correctly, every healthy walk succeeded. Postgres was the same within a few runs.

Per source on SQLite:

| Source | Runs | Result |
|---|---|---|
| USGS | 239 | 100 % ok |
| GeoNet | 109 | 100 % ok |
| Jobs fixture | 269 | 258 ok, 11 injected `empty` |
| Quakes fixture | 240 | 226 ok, 14 injected |
| Hacker News | 110 | 106 ok, 4 robots outages |
| Arbeitnow | 51 | 20 partial, 31 false degraded |

## 4. Leaks and growth

| Metric | Start → end (47 h) | Verdict |
|---|---|---|
| Worker RSS (SQLite / PG) | 32 → 40 MB / 45 → 40 MB, range 32–44 MB | flat, no leak |
| Worker file handles | 9 / 5, constant | no leak |
| Web app RSS | 43–66 MB, no trend | flat |
| Postgres container | 80 → 103 MB | flat after warm-up |
| SQLite `soak-jobs` DB | 0.6 → 7.7 MB | growth from Arbeitnow upserts (375 → 2,053 records; capped walks never replace, rows are pruned after `prune_after_days` = 14): by design |
| SQLite `soak-quakes` DB | 0.6 → 5.2 MB, flat after the first day | ok |
| Postgres DB | 8.9 → 12.9 MB | as above |
| `runs` table | about 500 rows a day per instance, never pruned | unbounded growth (D12) |
| Scratch directories `harvest-sbx-*` | 0 → 3, one per `kill -9` | leak (D10) |
| Postgres connections | 3–4 | flat |

## 5. Duplicates and stuck jobs

- **No overlapping runs** of any source in either instance during 47 h. Each source's run intervals were checked
  pairwise.
- **Duplicate execution: one case.** In #11 one job ran three times, one after another, because a worker with a
  skewed clock wrote its lease 2 h in the past and the real-clock web app's reaper re-queued it (D5).
- **Stuck jobs: none.** At the end there were no `running` runs and no `queued` or `running` jobs. Both killed
  runs were closed by the watchdog within 15 minutes.

## 6. Alerts

The webhook sink received 11 alert posts in 47 hours:

| Alert | Instances |
|---|---|
| zero and stale, jobs fixture (#1) | both |
| zero and stale, quakes fixture (#2) | both |
| stuck (#3) | both |
| stale, Hacker News (after #11) | SQLite only |

The alerts that should have been delivered and were not:

| Missing alert | Cause |
|---|---|
| zero for #4, #6 and the full redesign in #7 (both instances) | 24 h window per (source, kind), even across recovered incidents (D2) |
| stuck for the second kill (#10) | same: one stuck alert per source per 24 h (D2) |
| degraded for #7 (in the table, never sent) | the runner wrote it straight to the table (D1) |
| degraded for the capped API source (31 rows in the table, none sent) | false positive (D11), plus D1 |

No alert was sent that should not have been, except the false degraded rows, which were never sent. Repair
dispatch stayed off as configured.

## 7. Data correctness

- **Never-wipe held.** No injected failure deleted a row: 500, 429, a truncated document, a short read, 0 items,
  a redesign, a full disk, kills and clock jumps. Replaces only followed complete walks with at least 60 % of the
  baseline.
- **Ground truth.** At the end, the jobs fixture had 30 of 30 served ids stored, and the quakes fixture 45 of 45,
  with nothing missing and nothing extra. This was after 47 h of rotation (one item in, one out every 30 min) and
  dozens of replaces.
- **Dedup.** The We Work Remotely snapshot contains one duplicate guid. harvest stored 29 rows for the 30 served
  items, which is correct. `source_key` and `url` are unique per source in every table, and there were 0
  cross-source fingerprint duplicates in these projects.
- **Prune.** USGS replaced its rows every run (events fall out of the past-day window), and the pruned counts
  matched the feed changes. The capped Arbeitnow source never replaced, as intended.

## 8. Defects and fixes

| # | Defect (evidence) | Fix | Regression test |
|---|---|---|---|
| D1 | `degraded` and `review_stale` alerts, written by the runner straight into the table, were never delivered. The watchdog then treated them as already sent. (#7: table yes, webhook no.) | All alerts, including the runner's and the proxy budget's, go through `watchdog.raise_alerts`: dedup, then delivery. | `test_runner_degraded_alert_is_delivered_once_per_incident` |
| D2 | The 24 h re-alert window was per (source, kind), so a new incident after a recovery stayed silent, and a second stuck run went unreported. (#4, #6, #7 and #10 missed alerts.) | One alert per **incident**: a healthy run since the last alert ends the incident. `stuck` is keyed by run id. | `test_a_recurring_incident_is_alerted_again_after_a_healthy_run`, `test_each_stuck_run_is_alerted` |
| D3 | Delivery was fragile: an unwritable `alerts.jsonl` (full disk) raised before the webhook was tried; a webhook answering 500 counted as delivered; nothing was retried; `delivered` was never set. (Code reading after #5.) | Each channel is tried on its own; a non-2xx answer is a failure; pending alerts are retried on every check for 24 h; payloads carry the alert `id`; alerts from before the migration are marked delivered so they are never re-sent. | `test_failed_deliveries_are_retried_and_channels_are_independent`, `test_alerts_from_before_the_upgrade_are_not_resent` |
| D4 | A worker clock that ran ahead stamped runs in the future. Back on the real clock, `is_due` saw a "future" last run, so no source ran for 2 h 05 min (the size of the jump), and the stale check computed a negative age and stayed silent. (#9.) | Scheduling and the stale check ignore runs stamped more than 5 min in the future. A new watchdog finding, `clock_skew`, reports them. | `test_a_run_stamped_in_the_future_does_not_stall_the_schedule` |
| D5 | Job leases used each process's own clock. A worker behind the web app's clock had its job re-queued while it ran; the job ran three times and ended `lost`. A run job also kept walking after losing its lease. (#11.) | On Postgres, leases are written and compared on the server clock (`Store.clock_iso`). A run job stops its walk when its lease is lost (`cancel` reaches the runner and the sandbox), stores nothing more and ends `superseded`/`lease_lost`. On SQLite (one host) the host clock is authoritative. | `test_pg_job_leases_use_the_server_clock`, `test_a_run_job_whose_lease_is_lost_stores_nothing` |
| D6 | `extract.feed_items` returned `[]` for a document that did not parse, so a truncated feed was a **complete** walk with 0 rows. (#4: 3 of 3 marked complete.) | It raises `extract.FeedError` for a non-empty document that does not parse (empty input still gives `[]`). Lane detection stays lenient. The walk records a page error and the run is `error`, not complete. | `test_a_truncated_feed_is_an_error_not_a_complete_empty_walk`, `test_feed_parse_errors_are_errors_but_detection_stays_lenient`, `test_feeds_and_sitemaps` (updated) |
| D7 | A Postgres session killed by the server was never re-opened. Every later call of that `Store` failed ("the connection is closed"), and the web app answered 500 for that project until restarted. (#10, #12: 56 errors.) | A dead session is replaced before the next statement. A read is repeated once on the new session; a write fails once and is not replayed, because its outcome is unknown. | `test_pg_store_reconnects_after_its_session_is_killed` |
| D8 | Any exception outside a job ended the worker process; only systemd brought it back. (#10, #12; also about 500 restarts under libfaketime, see 9.) | The worker loop logs `worker_error`, backs off up to 60 s and continues. One project's broken store no longer stops the queue for the others (`queue_error`). | `test_the_worker_loop_survives_its_own_errors`, `test_one_broken_project_does_not_stop_the_queue` |
| D9 | A walk that stored nothing because its requests failed (500, a persistent 429, an unreachable robots.txt) was recorded as `empty`, and the zero alert read "(done: )". | Such a run is `error` with a summary, for example "no rows: 1 request(s) failed". A source that really has no items stays `empty`. | `test_a_walk_that_stored_nothing_because_requests_failed_is_an_error` |
| D10 | A walk killed with its parent (`kill -9`, OOM, a unit stopped mid-walk) left its `harvest-sbx-*` scratch directory behind: 3 in the soak. | A sibling `.owner` file, outside the child's view, names the parent. A starting worker removes the directories of dead owners on this host, and unmarked ones older than a day. It only removes this user's directories and never follows a symlink. | `test_scratch_dirs_of_dead_parents_are_swept` (including a planted symlink), `test_a_walk_leaves_nothing_in_the_temp_dir` |
| D11 | Without a replace baseline, "degraded" compared a run with the source's **row count in the table**. A capped source that never completes grew its table with every upsert walk and turned `degraded` once the table held twice one page: 31 false alerts. | `degraded` compares with the last complete walk's baseline or, without one, the median of the last five healthy runs (`Store.recent_yield`). The never-wipe replace rule is unchanged. | `test_a_capped_source_is_not_degraded_by_its_accumulated_rows` |
| D12 | `runs`, `quarantine`, `alerts` and `jobs` were never pruned: about 500 runs a day per instance. | The watchdog deletes finished history older than `HARVEST_HISTORY_DAYS` (90; 0 keeps everything). Records, sources and reviews are untouched. | `test_history_older_than_the_retention_is_pruned` |
| D13 | Lane detection classified a short server-rendered page without any script as a "JavaScript shell" (`browser`): the 15-item fixture page. | `browser` needs scripts on the page, or an explicit empty-root or "enable JavaScript" marker. | `test_a_short_page_without_scripts_is_html_not_a_javascript_shell` |
| — | Lane detection classified direct RSS and JSON-API URLs as `html` (found during setup). | Already fixed on `main` before this branch (lane `json_api`, a direct feed is lane `feed`); confirmed live. | (existing) |

## 9. Harness issues (not harvest)

These verdicts were wrong because of the test driver, not harvest.

- Under libfaketime, Python 3.12's `time.sleep` failed with `EINVAL`. The skewed workers therefore restarted about
  every 15 s in both clock windows, about 500 times in all. The clock findings still stand: the rows they wrote
  carry the skewed times, and D4 and D5 follow from the code and are reproduced by the regression tests. D8 now
  absorbs such errors.
- The SQLite disk-full "no-data-loss" FAIL was a false alarm: USGS replaced its own rows, and 436 → 434 was correct.
- The `tmp-leak` PASS verdicts were wrong: the driver listed directories after the walk had already created its
  own. The leak was found from the sampler.
- The Postgres "no-repair-dispatch" FAIL was a `%` quoting bug in the driver's SQL. Repairs were 0.
- In #11 the Postgres job could not be submitted at all: that is D7, not a lease result.
- The `pg_conn_loss/worker-continues` PASS counted runs that the +2 h worker had stamped in the future; the
  worker did restart (D8) and did collect again, from 07:05.
- `HARVEST_DATABASE=SQLite` was set as requested, but harvest does not read it. The store is chosen by
  `HARVEST_STORE_DSN` or the project's `store_dsn`.

## 10. Live re-verification of the fixes

The fixed code (this branch) ran on dev from 2026-10-03 12:30 to 14:44 UTC against the same fault fixture,
on a fresh SQLite home and a fresh Postgres database (`harvest_v`), with the same injections replayed.

| Check | Result | Evidence |
|---|---|---|
| Postgres sessions killed → web app (D7) | PASS | every request answered 200 on both projects throughout |
| Postgres sessions killed → worker (D7, D8) | PASS* | same worker PID, no restart. The driver's 7-minute window logged "runs after: 0" (FAIL), but a direct query of `harvest_v.runs` shows 44 runs after the kill (34 ok, 2 partial, 4 degraded, 2 empty, 2 error) |
| Degraded alerts reach table and webhook (D1, D11) | PASS | SQLite and Postgres: 2 in the table, 2 at the webhook (expected 2 and 2) |
| Truncated feed (D6, D9) | PASS | SQLite and Postgres: 2 truncated walks, both `error`, not complete; records 30 → 30 |
| Zero-rows alert recurrence (D2) | PASS | SQLite and Postgres: 2 zero alerts at the webhook (the truncate incident and the empty incident) |
| kill -9 → scratch-dir sweep and job retry (D10, D5) | PASS | 2 scratch dirs before the kill, none after the restart; one `sandbox_swept` line; job done with attempts 2; no surviving processes |
| Clock 2 h ahead, then back (D4) | PASS | no source with a cadence ≤ 30 min was silent 35 min after the clock returned; 6 `clock_skew` alerts at the webhook; 0 `worker_error` lines |
| Postgres lease skew (D5) | PASS | job finished once (attempts 1) |

\* The one FAIL line in `soak.log` is a harness artefact, settled by the database query above.

## 11. Known limits after this work

- On SQLite, job leases use the host clock. Processes sharing one SQLite file are on one host, so they share it;
  a host whose clock steps still has a short window. Postgres uses the server clock.
- Delivery is at least once. A receiver can see an alert twice after a partial failure, and should deduplicate on
  `id`.
- A run that loses its job lease stops at the next page or poll; requests already in flight finish.
