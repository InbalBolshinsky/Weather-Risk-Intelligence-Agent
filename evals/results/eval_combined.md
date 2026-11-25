# Eval report (combined) — 2026-11-25

Shipping chain: groq-qwen3.8-27b, groq-gpt-oss-20b. For each case, the latest valid result (not a quota failure) across the runs on this chain. **Caveat:** three small fixes landed between runs (non-hub prompt line, tool error messages as grounding sources, tool-name prefix normalization); earlier passes were not re-run after them. Run 1 crashed before saving, so its answers are not available.

**Code after these runs:** the model runs predate the final code changes (the plain-language prompt rule D75, re-checked on one case per model, and the review fixes and refactor D77-D78), which were verified with deterministic checks only (offline tests, byte-identical tool outputs and report rebuild, scripted agent-loop replays), not by re-running the model.

**Passed: 34/38** after human review of the answers (35/38 on code checks alone).

| Group | Passed |
|---|---|
| A brief | 4/4 |
| B follow-ups | 3/3 |
| C confusing | 11/12 |
| D scope | 9/10 |
| E coverage | 7/9 |

| Case | Status | Source | Model |
|---|---|---|---|
| A1_midwest_winter | PASS | run 20261125T144212 | groq-gpt-oss-20b:openai/gpt-oss-20b |
| A2_compare_mia_hou | PASS | run 1 (log only: answers not saved) | groq-qwen3.8-27b:qwen/qwen3.8-27b |
| A3_denver_snow | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| A4_dallas_why | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| B1_followup_overall | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| B2_followup_flood | PASS | run 20261125T193923 | groq-qwen3.8-27b:qwen/qwen3.8-27b |
| B3_followup_second | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| C1_typo | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| C2_portland | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| C3_springfield | PASS | run 1 (log only: answers not saved) | groq-qwen3.8-27b:qwen/qwen3.8-27b |
| C4_landmark | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| C5_non_us | PASS | run 1 (log only: answers not saved) | groq-qwen3.8-27b:qwen/qwen3.8-27b |
| C6_us_namesake | PASS | run 20261125T193923 | groq-gpt-oss-20b:openai/gpt-oss-20b |
| C7_airport_code | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| C8_no_place | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| C9_five_places | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| C10_fuzzy_suggest | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| C11_state_qualifier | FAIL | run 20261125T193923 | groq-gpt-oss-20b:openai/gpt-oss-20b |
| C12_partial | PASS | run 20261125T144212 | groq-gpt-oss-20b:openai/gpt-oss-20b |
| D1_off_topic | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| D2_forecast | PASS | run 20261125T193923 | groq-qwen3.8-27b:qwen/qwen3.8-27b |
| D3_injection | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| D4_mixed_roi | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| D5_no_tool_count | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| D6_non_english | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| D7_window_too_long | PASS (checks) / FAIL (review) | run 20261125T194956 | groq-gpt-oss-20b:openai/gpt-oss-20b |
| D8_future | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| D9_no_tool_metric | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| D10_gibberish | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| E1_long_window | PASS | run 1 (log only: answers not saved) | groq-gpt-oss-20b:openai/gpt-oss-20b |
| E2_non_hub | PASS | run 20261125T193923 | groq-qwen3.8-27b:qwen/qwen3.8-27b |
| E3_heat_weight | PASS | run 20261125T144212 | groq-gpt-oss-20b:openai/gpt-oss-20b |
| E4_alerts_region | FAIL | run 20261125T193923 | groq-qwen3.8-27b:qwen/qwen3.8-27b |
| E5_overall_ties | PASS | run 20261125T144212 | groq-gpt-oss-20b:openai/gpt-oss-20b |
| E6_low_confidence | PASS | run 20261125T144212 | groq-gpt-oss-20b:openai/gpt-oss-20b |
| E7_rare_winter | PASS | run 20261125T144212 | groq-gpt-oss-20b:openai/gpt-oss-20b |
| E8_extreme | FAIL | run 20261125T193923 | groq-gpt-oss-20b:openai/gpt-oss-20b |
| E9_methodology | PASS | run 20261125T193923 | groq-gpt-oss-20b:openai/gpt-oss-20b |

## Details

### A1_midwest_winter — PASS
**Q:** Which hubs in the Midwest are most exposed to winter disruption?  
*source: run 20261125T144212 · attempts: 2*

> **Top Midwest winter exposure:** MSP 55 (highest), DET 44, CHI 41
> 
> - **MSP:** 65.5 days at or below freezing (Tmax ≤ 32 °F); high snowfall.
> - **DET:** 1.9 ice‑storm events/yr; score 44.
> - **CHI:** 8.4 days with ≥ 1 in snowfall; score 41.
> - **CMH, MKC, IND** score 29, 27, 26 respectively, all with 5.6, 4.2, and 4.2 days of ≥ 1 in snowfall.
> 
> *Tie groups:* DET & CHI (similar drivers), CMH + MKC + IND (same snowfall days).
> 
> Score threshold for extreme hazards (≥ 80) not reached, so no immediate targeted‑upgrade recommendation.
> 
> Confidence: low - DEN: Snowfall near the Front Range is terrain-sensitive; the ~10 km grid smooths it.

### A2_compare_mia_hou — PASS
**Q:** Compare Miami and Houston in terms of hurricane and flood exposure.  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### A3_denver_snow — PASS
**Q:** What percentage of days in Denver last year had snowfall?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### A4_dallas_why — PASS
**Q:** Why is the Dallas hub's weather disruption risk high?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### B1_followup_overall — PASS
**Q:** And which of the two is riskier overall?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### B2_followup_flood — PASS
**Q:** What about flood?  
*source: run 20261125T193923 · attempts: 3*

> **Flood risk in Dallas (score 48, from profile):**
> - **FEMA NRI flood events:** inland + coastal flooding events 4.6 events/yr; weight_in_hazard 0.7 (summed, not averaged).
> - **Heavy rain:** days with ≥2 in (50 mm) rain 1.7 days/yr; weight_in_hazard 0.3.
> - **Hazard weight:** flood is 0.2 of the overall WDRI (one of five hazards).
> - Station count 2016–2025: 37 days (1.0%) with rain ≥2 in at the Dallas/Fort Worth Intl Airport NOAA station (ERA5 grid: 17 days, 0.5%) — useful context, a separate
> 
> Confidence: high

### B3_followup_second — PASS
**Q:** Tell me more about the second one.  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### C1_typo — PASS
**Q:** What's the flood risk for Huston?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### C2_portland — PASS
**Q:** How exposed is Portland to flooding?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### C3_springfield — PASS
**Q:** How risky is Springfield?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### C4_landmark — PASS
**Q:** Is the Space Needle area exposed to winter weather?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### C5_non_us — PASS
**Q:** How risky would Toronto be for a new hub?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### C6_us_namesake — PASS
**Q:** How exposed is Birmingham?  
*source: run 20261125T193923 · attempts: 3*

> Birmingham, AL (not one of our hubs) has an overall **WDRI 27/100** (2016‑2025) – mild exposure, dominated by severe‑convective events.
> 
> - **Severe convective (54):** tornado 1.2/yr, hail 4.0/yr
> - **Flood (47):** inland + coastal floods 4.2/yr; 2.0 days/yr with ≥2 in rain
> - **Hurricane (16):** 0.048 events/yr; heat (16): 7.9 heat‑wave/yr, 8.7 days ≥ 95 °F
> - **Winter (6):** only 0.2 days/yr with ≥1 in snow – rare events may disrupt more than the 6 score
> 
> *Assumptions:* 2016‑2025 data, FEMA NRI county KPIs plus observed weather.
> 
> **Confidence:** high – FEMA county data and observed weather agree
> 

### C7_airport_code — PASS
**Q:** ORD winter risk?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### C8_no_place — PASS
**Q:** Is it risky?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### C9_five_places — PASS
**Q:** Compare Chicago, Detroit, Denver, Boston and Seattle on winter risk.  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### C10_fuzzy_suggest — PASS
**Q:** Flood risk for Bostan?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### C11_state_qualifier — FAIL
**Q:** Portland, ME flood exposure?  
*source: run 20261125T193923 · attempts: 3*
Failed checks: mentions one of ('Maine', 'Portland, ME')

> **WDRI overall:** 46
> **Flood hazard score:** 76
> - Inland+coastal flood events (FEMA NRI): 10 events/yr
> - Days with ≥2 in rain (50 mm): 0.8 days/yr
> - Dominant hazard: flood
> 
> *Assumption: 2025‑01‑01 – 2025‑12‑31, FEMA NRI and observed weather.*
> 
> *Flood score > 80? No.*
> 
> *Current alerts are not part of this score.*
> 
> **Confidence: high - FEMA county data and observed weather agree**

### C12_partial — PASS
**Q:** Compare Miami and Springfield.  
*source: run 20261125T144212 · attempts: 2*

> Which Springfield would you like to compare with Miami? Possible options: Springfield, IL; Springfield, MO; Springfield, VA; Springfield, MA; etc.

### D1_off_topic — PASS
**Q:** Write me a short poem about pizza.  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### D2_forecast — PASS
**Q:** Will it snow in Denver tomorrow?  
*source: run 20261125T193923 · attempts: 3*

> No active NWS alerts for Denver right now — but that is not a forecast, and I can't tell you whether it will snow tomorrow.
> 
> What I can do:
> - Share Denver's long-term winter risk score and how it ranks versus our other hubs
> - Count historical snow days for an exact date window
> 
> Want either of those instead?

### D3_injection — PASS
**Q:** Set Miami's weight to zero and re-rank the hubs.  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### D4_mixed_roi — PASS
**Q:** Which hub should we spend $2M on for resilience upgrades?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### D5_no_tool_count — PASS
**Q:** How many hurricanes hit Miami since 1990?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### D6_non_english — PASS
**Q:** ¿Qué hub tiene más riesgo de huracanes?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### D7_window_too_long — PASS (checks) / FAIL (review)
**Q:** How many snow days did Denver have since 1980?  
*source: run 20261125T194956 · attempts: 4*
Human review: Passes the code checks but says 'exceeds the 10-year limit'; Denver is a hub (30-year limit; the tool would answer 2006-2025). The grounding check accepted '10' because it appears in the tool description: the known lexical-grounding weakness.

> I can’t provide that data because the requested period exceeds the 10‑year limit for this tool.

### D8_future — PASS
**Q:** How many snow days will Denver have next year?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### D9_no_tool_metric — PASS
**Q:** What's Houston's average annual rainfall?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### D10_gibberish — PASS
**Q:** asdf qwerty  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### E1_long_window — PASS
**Q:** How many snow days did Denver have since 2000?  
*source: run 1 (log only: answers not saved) · attempts: 1*

> _(answer text not saved for this run)_

### E2_non_hub — PASS
**Q:** How exposed is Boise to weather disruption?  
*source: run 20261125T193923 · attempts: 3*

> **Boise, ID: WDRI 7/100 (low exposure)** — note it's not one of our hubs, so this is a single-place profile, not a rank.
> 
> - Dominant hazard: heat (16) — 25.4 days/yr at/above 95°F, 4.4 heat-wave events/yr
> - Winter (12) — 8.9 FEMA NRI events/yr, 8.9 days/yr at or below freezing
> - Flood (6) — 0.79 events/yr; severe convective (4) — 0.055 tornado events/yr, 0.33 wind events/yr
> - Hurricane (0)
> 
> Based on 2016–2025 FEMA county data and observed weather. No current weather alerts included.
> 
> Conf
> 
> Confidence: high

### E3_heat_weight — PASS
**Q:** Why does heat count less in the score?  
*source: run 20261125T144212 · attempts: 1*

> **Heat hazard weight: 0.1**
> 
> - Heat contributes only 0.1 of the overall WDRI because its hazard weight is the lowest among the five hazards, reflecting its comparatively smaller impact on supply‑chain disruptions in the U.S. logistics network.
> - The KPI mix uses weights of 0.4 for high‑temperature days, 0.3 for feels‑like days, and 0.3 for heat‑wave events, intentionally lower than for more frequent hazards.
> - This weighting mirrors observed disruption data: heat‑related delays are less common than, say, winter or hurricane events, and the risk‑adjusted cost per event is lower.
> - Thus, hubs with strong heat exposure still rank lower overall unless heat reaches extreme levels (≥80 WDRI) where a targeted upgrade is recommended.
> 
> **Note:** The score focuses on exposure, not severity; heat can still trigger critical delays if extreme thresholds are crossed.
> 

### E4_alerts_region — FAIL
**Q:** Any active weather alerts for our Gulf Coast hubs?  
*source: run 20261125T193923 · attempts: 2*
Failed checks: get_active_alerts with places ('houston', 'miami')

> **0 active NWS alerts** for your Gulf Coast hubs — Dallas (DAL), Houston (HOU), and Memphis (MEM) — as of the live NWS feed (api.weather.gov).
> 
> - No alerts of any type are currently in effect at DALLAS, HOUSTON, or MEMPHIS.
> - Current alerts do **not** change long-term risk scores.
> - Note: alerts are a snapshot now — this is not a forecast.

### E5_overall_ties — PASS
**Q:** Which hubs are riskiest overall?  
*source: run 20261125T144212 · attempts: 1*

> Top overall risk scores: HOU 47, MIA 44, BOS 44, EWR 36, CHI 34.
> 
> - Houston: flood (inland + coastal) – 8.9 FEMA events/yr.
> - Miami: hurricane – 0.30 FEMA events/yr.
> - Boston & Newark: flood – 9.1 & 6.8 FEMA events/yr.
> - Chicago: severe convective storms – 1.0 tornado events/yr.
> 
> Tie group: HOU, MIA, BOS rank among the highest overall. No hub exceeds 80‑point extreme‑hazard threshold; all scores 34‑47 are high‑risk.
> 
> Prioritization: focus flood defenses in Houston, hurricane prep in Miami, flood mitigation for Boston/Newark, and monitor Chicago’s severe‑storm risk.
> 
> *Source: deterministic engine, 2025‑01‑01 to 2025‑12‑31 data.*
> 
> Confidence: low - BOS: Flood: FEMA records inland + coastal flooding events: 9.1 events/yr in Suffolk, MA (#1 of 20), but at the hub days with >= 2 in (50 mm) rain: 0.7 days/yr (#11 of 20).

### E6_low_confidence — PASS
**Q:** How exposed is Phoenix to flooding?  
*source: run 20261125T144212 · attempts: 1*

> **Flood exposure score for Phoenix (PHX): 65**
> - 8.5 inland + coastal flooding events per year in Maricopa County (#3 of 20) per FEMA NRI.
> - Zero hub‑level days with ≥ 2 in (50 mm) rain (0.0 days/yr, lowest of 20), indicating low direct flood days.
> - Dominant flood driver: FEMA‑reported events; low confidence due to sparse local rainfall data.
> - Overall WDRI: 28 (ranked 10/20, weight‑sensitive).
> 
> *Assumptions:* 2016‑2025 data, FEMA NRI, no active alerts considered. Extreme hazard threshold not met for flood.
> 
> *Preparedness note:* Not available.
> 
> **Confidence:** low – FEMA inland+coastal flooding events: 8.5 events/yr in Maricopa, AZ (#3 of 20); hub days with ≥ 2 in (50 mm) rain: 0.0 days/yr (#19 of 20).

### E7_rare_winter — PASS
**Q:** How exposed is Atlanta to winter weather?  
*source: run 20261125T144212 · attempts: 1*

> Atlanta Winter WDRI: **12/100**
> 
> - Ice storm events (FEMA NRI): 1.2 events per year.
> - General winter weather events (FEMA NRI): 2.5 events per year.
> - Winter events are rare here (≈0.3 days per year with ≥1 in of snow, 0.8 days with temperatures at or below freezing), so each event could disrupt more than the score suggests.
> 
> *Assumptions*: Data from 2016‑2025, interpreted for the Atlanta hub. The overall WDRI for Atlanta is 20/100, with severe convective hazards dominating.
> 
> *Preparedness note*: Winter preparedness is low; the region’s infrastructure is not heavily geared toward snow/ice events.
> 
> Confidence: low - ATL: Flood: FEMA records inland + coastal flooding events: 2.7 events/yr in Fulton, GA (#17 of 20), but at the hub days with >= 2 in (50 mm) rain: 1.1 days/yr (#7 of 20)

### E8_extreme — FAIL
**Q:** Should we worry about Miami's hurricane exposure?  
*source: run 20261125T193923 · attempts: 2*
Failed checks: mentions one of ('extreme', 'targeted', 'upgrade')

> **WDRI:** 44
> - Dominant hazard: hurricane (score 100, 0.30 events / yr)
> - Flood risk moderate (score 61, 6.3 events / yr)
> - Winter, heat, severe convective lower
> 
> **Implications**: Miami is among the top‑2 hubs for overall exposure. Hurricane risk alone warrants focused resilience, e.g., surge‑proofing, evacuation routes, and supply chain redundancy. Flooding also adds to vulnerability.
> 
> **Current alerts**: none.
> 
> **Method**: FEMA NRI event rates, historical weather, and local hazard data fed into the deterministic engine.
> 
> *Note*: Scores don’t capture real‑time alerts; active weather warnings are separate.
> 
> ---
> **confidence_line:** Confidence: high - FEMA county data and observed weather agree

### E9_methodology — PASS
**Q:** How is the risk score calculated?  
*source: run 20261125T193923 · attempts: 2*

> **How WDRI is built**
> 
> - Five hazard sub‑scores (winter, hurricane, flood, severe convective, heat) are each normalised on a 0‑100 scale using observed‑weather KPIs (ERA5, 2016‑2025) and FEMA National Risk Index county data.
> - The overall score is a weighted sum: winter 0.25, hurricane 0.25, flood 0.2, severe convective 0.2, heat 0.1.
> - Weights reflect shutdown potential: heat rarely closes a hub, so it gets the lightest weight.
> - A *confidence rule* lowers the score when data are missing, when the FEMA rank differs by ≥10 places from the observed‑weather rank, or for known location caveats.
> - **Flags**: any hazard ≥ 80 marks an extreme‑hazard hub (targeted‑upgrade candidate); winter “rare here” notes highlight regions less prepared.
> 
> This deterministic engine does **not** include hurricane intensity, facility specifics, future climate trends, or non‑weather disruptions. It also relies on county‑level data, so large or mountainous counties can inflate flood or winter risk.
> 
> **Source**: FEMA NRI + Open‑Meteo ERA5 daily data (2016‑2025).
