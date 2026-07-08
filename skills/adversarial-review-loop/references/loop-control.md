# Loop Control

Default loop limits:

- L0-L2: one review pass unless the user asks for iteration.
- L3: at most 2 auto-apply/review iterations.
- L4: at most 3 iterations; after that, summarize remaining risk and ask.

Stop when:

- no new HIGH/MED findings remain
- every finding has disposition evidence
- applied fixes have verification evidence whose assertion strength would catch the original finding's failure mode
- validation required by the review packet is passed, blocked with exact reason, or deferred with owner and residual risk
- skipped reviewers have reasons
- residual risk is stated

Escalate to the user when:

- the same blocker repeats twice
- new LOW findings keep appearing after iteration 2
- formatter or refactor changes expand the diff beyond the review packet
- a hard-stop surface is detected
- validation fails or cannot run
- a mid-loop request changes scope, autonomy, hard stops, validation requirements, or expected side effects

Use a duplicate key for repeated findings:

```text
<category>:<location-or-surface>:<failure-mode>
```
