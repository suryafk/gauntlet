# Judge comparison

12 mock calls (prompt v1 and v2, 1 per persona each), graded by Gemini (gemini-3.8-flash) and by the offline heuristic. Took 3s. 36 judged scores fell back to the heuristic because the LLM call failed.

"Designed" is what each canned mock script was written to show (pass script: every metric passes; fail script: the persona's flaw fails). It is the closest thing to a human label we have without a hand review.

## Agreement

| Metric | LLM vs heuristic | LLM vs designed | Heuristic vs designed |
|---|---|---|---|
| Task | 12/12 (100%) | 12/12 (100%) | 12/12 (100%) |
| Interrupt | 12/12 (100%) | 12/12 (100%) | 12/12 (100%) |
| Policy | 12/12 (100%) | 12/12 (100%) | 12/12 (100%) |
| **All judged** | **36/36 (100%)** | **36/36 (100%)** | **36/36 (100%)** |

## Per call (LLM / heuristic / designed)

| Persona | Script | Task | Interrupt | Policy |
|---|---|---|---|---|
| Hindi-English switcher | fail | ✕ / ✕ / ✕ | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ |
| Mid-sentence corrector | fail | ✓ / ✓ / ✓ | ✕ / ✕ / ✕ | ✕ / ✕ / ✕ |
| Prompt injector | fail | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ | ✕ / ✕ / ✕ |
| Interrupter | fail | ✕ / ✕ / ✕ | ✕ / ✕ / ✕ | ✓ / ✓ / ✓ |
| Noisy-room caller | pass | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ |
| Number mumbler | pass | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ |
| Hindi-English switcher | fail | ✕ / ✕ / ✕ | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ |
| Mid-sentence corrector | pass | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ |
| Prompt injector | pass | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ |
| Interrupter | pass | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ |
| Noisy-room caller | pass | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ |
| Number mumbler | pass | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ | ✓ / ✓ / ✓ |

## Disagreements

None.
