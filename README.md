# Weather-Risk-Intelligence-Agent

An AI agent that helps logistics analysts decide which US distribution hubs are most exposed to weather
disruption. A deterministic engine computes every score; the LLM only picks tools and explains. Full design
and decision log: [PLAN.md](PLAN.md). Setup and run commands: [CLAUDE.md](CLAUDE.md) (full README in step 8).

## Known limitations

- **LLM availability (free tier only).** The main model (Groq `qwen3.8-27b`, 14/16 in our comparison) is a
  Groq "preview" model that can be withdrawn; the backup (`gpt-oss-20b`, 9/16) answers fewer questions
  correctly. Groq's free tier allows ~8,000 tokens/min per model, so back-to-back questions may wait up to
  ~30 s. Gemini's free tier failed (rate limits, empty 400 errors) and is disabled.
- **Grounding is lexical.** Every number in an answer must appear in a tool result, but the check can't tell
  whether a correct number is attached to the right hub or hazard, and claims without numbers aren't checked.
  The eval set covers this; the code doesn't. Example from the evals: an answer said "exceeds the 10-year limit"
  for a hub (the limit is 30 years); "10" passed because it appears in the tool description.
- **Eval result: 34/38** ([report](evals/results/eval_combined.md)). Besides the case above: one geography slip
  (inland hubs called "Gulf Coast") and two rule-following slips by the fallback model (not naming a non-hub
  place; not flagging an extreme hazard).
- **The score can't be validated against real closures.** Thresholds, scales and weights are documented
  judgment calls (no company disruption data). FEMA data is county-level (large or mountainous counties can
  inflate flood/winter frequencies; such cases are flagged as low confidence). Hurricane scores count events,
  not intensity (Boston's county equals Houston's). Hubs are placed at metro centers, not real facilities.
- **Some behaviors rely on the prompt only:** staying in scope, treating text inside tool results as data,
  and not making the investment decision. Covered by eval cases, not enforced in code.
- **Every data answer costs at least two model calls** (tool call, then the final `submit_answer`), because
  Groq can't combine JSON-schema output with tool use.
- **Data operations are manual.** The cache is refreshed with `scripts/refresh_data.py`. Open-Meteo's free
  tier is for non-commercial use (a company deployment needs the paid tier), and its 10,000 calls/day are
  shared by all users. FEMA's service could change its fields (the refresh fails loudly if it does).
