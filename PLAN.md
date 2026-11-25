# PLAN — decision log

Working log of the Weather Risk Intelligence Agent. Design: [DESIGN.md](DESIGN.md) · run: [README.md](README.md) ·
working mode: [CLAUDE.md](CLAUDE.md).

**Status (2026-11-26):** done: API verification, data + cache, scoring, agent, API + chat page, evals (34/38),
deploy (Render), docs, bonus score-change alerts. Open: export the AI session; LLM-dependent review items (D77).

Every decision was made by the user after discussion, unless marked. "—" = no alternative was recorded.

| # | Decision | Alternatives considered | Why |
|---|---|---|---|
| D1 | Deterministic scoring engine, separate from the agent; the LLM never produces numbers | LLM computes or ranks | Reproducible, testable numbers |
| D2 | Own loop behind one provider interface (superseded by D21) | — | — |
| D3 | SQLite (cache, FEMA table, turns) + YAML config | Postgres; JSON files | One writer, tiny data, zero ops; sessions and counters are queried |
| D4 | Fixed anchors for observed KPIs, national percentiles for FEMA (refined by D30, D35) | — | Absolute scales: adding a hub changes no score |
| D5 | FEMA NRI annualized event frequency | NRI risk score; expected annual loss | Risk score includes social vulnerability; losses scale with county building value |
| D6 | NWS alerts are a live layer, never in the score | — | The score is long-run exposure |
| D7 | Storm tracks (IBTrACS/HURDAT2) demoted to stretch; not done | Hurricane-intensity KPI | Time; weakness documented (D33) |
| D8 | Weighted mean + dominant hazard + weight-sensitivity flag (superseded by D36, D37) | — | — |
| D9 | Heat added as the 5th hazard | Four hazards | User request |
| D10 | 20 hubs; any US place profiled on demand | Precomputed all-US city table | Open-Meteo's free 10K calls/day can't cover ~19K places |
| D11 | Deploy to a URL with an access code and caps (host chosen in D72) | Local run only | The brief prefers a URL |
| D12 | Final answer via a strict terminal `submit_answer` tool, `tool_choice=required` | JSON response format | Groq can't combine JSON-schema output with tools (400) |
| D13 | Next model only on availability errors or two malformed calls, never on validation failures | Fallback on a bad answer | Bad answers are repaired, not re-rolled |
| D14 | Snow days at ≥ 0.1 in (NWS measurable) and ≥ 1 in (NOAA normals) | One threshold | Both are official definitions |
| D15 | Own loop, no LangChain/LangGraph | LangChain, LangGraph | The checks are custom anyway; one readable file; fewer dependencies |
| D16 | Pin `era5_seamless`; cache history permanently; throttle refresh by rate-limit headers | Unpinned model | An unpinned 10-year request hung over 2 minutes |
| D17 | Null FEMA frequency = not applicable → 0 | — | FEMA leaves hazards that don't apply empty |
| D18 | Cold wave excluded from winter | Include it | Defined relative to local norms |
| D19 | Hub weather questions: airport station first, grid alongside; scores use the grid | Grid only; station only | Stations are measured; the grid covers every hub the same way |
| D20 | Flexible timeline, no pre-emptive cuts | Cut list | User decision |
| D21 | Groq free tier through the `openai` SDK (Chat Completions); "Grok" was a typo | xAI, OpenAI | Free; one adapter for any OpenAI-compatible vendor |
| D22 | Prompt: analyst persona, numbers first, English only, hub list in the prompt | — | — |
| D23 | Tools return display-ready numbers and computed comparisons; prompt caching not relied on | The model calculates | The model never does arithmetic |
| D24 | Tools resolve names themselves (no `resolve_location`); ≤ 4 places per call; `get_methodology(topic)`; non-hub cap | A `resolve_location` tool | Resolution can't be skipped or guessed; fewer calls; fits the token budget |
| D25 | Place handling by `place.status`; fuzzy ≥ 85 accept, 70–85 ask; non-US out of scope (refined by D48) | — | — |
| D26 | Forecasts: say so, but check alerts; mixed requests: answer the in-scope part | — | Useful partial answers |
| D27 | History: latest turn in full, older turns as question + answer | Full history; answers only | Follow-ups need prior results; token limits |
| D28 | Confidence level set by code | Model-stated | Reproducible |
| D29 | Prompt answer-shape details (partly superseded by D41–D43) | — | — |
| D30 | FEMA frequencies scaled linearly to a national percentile county | Percentile ranks | Ranks inflated tiny frequencies |
| D31 | Confidence cross-check is rank-based (≥ 10 places apart); severe storms excluded | Absolute points | The absolute check flagged 29 of 100 scores |
| D32 | Multi-part FEMA hazards summed; 0 events → 0 | — | — |
| D33 | KPIs = FEMA frequency + observed weather, cross-checked; hurricane intensity left out (documented) | FEMA only; observed only | Two independent sources; intensity needs storm tracks (no time) |
| D34 | Fixed national thresholds + winter-only "rare here" note that never changes the score | Region-relative scores; a mix; a heat note too | Keeps hubs comparable while showing preparedness; a heat note would mark 9–14 of 20 hubs |
| D35 | FEMA scale top = national p99 | p95 | p95 capped 25 hub values at 100; p99 caps 4 |
| D35a | Heat stays in the WDRI | Drop heat | User decision (lowest weight) |
| D36 | Judgment weights 0.25/0.25/0.20/0.20/0.10 + weight-sensitivity flag | Equal weights; FEMA loss weights | Equal makes heat = hurricane; FEMA weights give winter 1%, flood 62% |
| D37 | Weighted mean + extreme-hazard callout (≥ 80) | Worst hazard; blends; power mean | Additive explanation; extremes stay visible |
| D38 | Model order chosen by a comparison through the real agent | — | Measured on this task |
| D39 | Run the comparison once, after the step-4 decisions | Run it at once | No wasted quota |
| D40 | All models rate-limited: wait once (≤ 30 s), then retry; gpt-oss at low reasoning | — | Per-minute limits refill quickly |
| D41 | The UI shows only the answer text; raw tool results in `debug` | Show calculations | Analysts need the answer |
| D42 | ≤ 150 words, ≤ 200 for rankings and comparisons | — | — |
| D43 | Confidence and assumptions written in the text; the stated level is validated; separate fields dropped | Separate fields | Saves tokens; one text for the user |
| D44 | Grounding with no small-number exemption | Exempt small numbers | Small numbers can be wrong too |
| D45 | Weather-stat window trimmed to the last day with data | Fixed lag constant | Measured lags differ (grid 7 days, station 10) |
| D46 | Hub stats up to 30 years (station); grid only inside 2016–2025; other places ≤ 10 years | — | A 30-year grid download costs ~470 Open-Meteo calls |
| D47 | Boston alias "logan" → "logan airport" / "boston logan" | — | Logan, UT exists |
| D48 | A same-name US city ≥ 50K beats a larger foreign one | Largest match wins | Birmingham means Alabama here |
| D49 | Limits kept with evidence: 25 non-hub profiles/day, 20% ambiguity ratio, 50K namesake, no fuzzy under 4 letters | — | Each measured or tested |
| D50 | Numbers still unverified after the repair → code appends a note | Reject the answer | The answer stays usable and honestly flagged |
| D51 | Confidence line still wrong after the repair → code writes it | — | Gap found while coding |
| D52 | Low/medium confidence states the conflicting facts; the validator checks their numbers | Level only | Analysts must know why |
| D53 | Free-tier models only (Cerebras out) | Paid models | User constraint |
| D54 | Token savings: ready confidence line, top-5 rankings, lean results, smaller contract, tighter prompt | — | Groq allows 8K tokens/min |
| D55 | Costs and ROI out of scope | An ROI rule | Not in the data |
| D56 | Chain qwen3.8-27b → gpt-oss-20b | gpt-oss-120b (5/16), Gemini 3.6 flash (0/16), 3.5 flash-lite (1/16) | 16-question comparison: 14/16 and 9/16 |
| D57 | Numbers shipped as unverified (and validator feedback) never become sources later | — | Closed a grounding loophole |
| D58 | Weakness review: fix grounding now (D57), per-request DB connections, stronger evals; the rest documented | — | — |
| D59 | Demo protected by an access code (`DEMO_TOKEN`, only in the host), sent as a link | User accounts | Protects the LLM quota; the repo is public |
| D60 | 30 questions per conversation | — | — |
| D61 | ~80 questions per day, reset at 00:00 UTC | — | Below Groq's daily quotas |
| D62 | One static HTML page served by FastAPI | — | Enough for the demo |
| D63 | Endpoints: page, chat, status, LLM-free rankings, `/docs`; only chat needs the code | — | Only chat spends LLM quota |
| D64 | 38 end-to-end eval cases in 5 groups | — | Coverage of the brief, follow-ups, confusing input, scope, data |
| D65 | Grading in code with pairing checks; every answer printed for human review | LLM-as-judge | Cost |
| D66 | Re-run failed cases once (flaky vs real failure) | — | — |
| D67 | gpt-oss-20b at `reasoning_effort: low` (unverified) | Default effort | Fewer output tokens |
| D68 | Eval report = latest valid result per case across the shipping-chain runs, labelled by run | One full re-run | Free-tier quota |
| D69 | Skip a rate-limited model until its retry time (deferred; the message part done in D74) | — | — |
| D70 | Station gaps: use only the years the station measured, and say so; "no data" is not zero | — | Found by eval case D7 |
| D71 | Eval 34/38; remaining failures documented as model limitations, no code change | — | — |
| D72 | Render free tier, native Python, committed snapshot, secrets only in Render, one worker, accept the 15-min sleep | HF Docker Spaces (paid); Fly/Railway/Koyeb (no free tier or card); deploy-time refresh (~3.1K calls); keep-awake ping; a backup host | Free and simple; the local run is the fallback |
| D73 | Docs split by deliverable; chat page wake-up retry, 2-min timeout, waiting messages | — | — |
| D74 | Long rate limit (e.g. daily tokens): fail at once and tell the real wait | Wait 30 s first | That wait can't succeed |
| D75 | Plain language: weights explained only when asked, in plain words with the per-hazard ranking offered; no internal terms in answers | Keep the technical wording | Analysts can read it (two proposed options combined) |
| D76 | Score-change alerts: event-triggered; committed baseline; ≥ 5 points or crossing 80; Slack/Discord/JSON payload; nothing in the UI | Schedule; SQLite baseline; any change; UI notice; agent tool | Scores change only on data/config updates; the host wipes SQLite |
| D77 | Code review: offline-verifiable fixes only (percentile text, `top_n` bounds, quota refund, byte-safe code check, UTC caps, safe errors, `#code=` link, Open-Meteo cap) | — | LLM-dependent items deferred: no tokens |
| D78 | Refactor with no behavior change (loop and tool helpers); proved byte-identical | — | — |
| D79 | Docs tightened (647 → 360 lines): each fact in one place; DESIGN gains a 10-row key-decisions table, scope and assumptions; PLAN = decision log; README = run instructions; generated eval reports left unedited | A 22-row key-decisions table; editing the eval reports | Shorter to read; the reports are the evidence of each run |
| D80 | When every model failed with malformed tool calls (no rate limit or outage), say "The AI model couldn't complete this request. Please try again or rephrase it."; rate-limit, outage and mixed failures keep the D74 messages | Keep "busy" for every failure | Found 2026-11-26: gpt-oss-20b's schema rejects were shown as "busy (free-tier limits)"; in a mix, capacity is part of the problem, so "try again later" stays right |
