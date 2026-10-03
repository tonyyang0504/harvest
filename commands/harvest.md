---
description: Collect data for a target in some regions — census, scrapers, review, collection, schedule
argument-hint: <target> in <regions> [as <record type>]
---

Collect **$ARGUMENTS** with harvest. Load the `harvest` skill and follow its flow:

1. Parse the target, the regions (ISO codes, country names or groups such as EU, GCC, CAUCASUS) and the record type.
   Use `harvest_templates` if the type is unclear. Then call `harvest_new_project`.
2. Dispatch the `harvest-census` agent, then the `harvest-auditor` agent.
3. Call `harvest_detect_lane` for all candidates. Dispatch one `harvest-builder` agent per source that has a lane.
4. Show the user the source list with lanes, terms and review verdicts. Call `harvest_enable_source` for the sources they approve.
5. Call `harvest_run`, then `harvest_status`. Offer `harvest_export`, and offer `harvest_schedule` for recurring collection.

Policy: public pages only. Never log in, solve captchas, get past paywalls or use mirrors. Terms that forbid collection mean lane `none`.
