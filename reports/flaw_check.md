# Flaw check

> **Read this first.** Incomplete run. The free Gemini quota ran out after about 2 of the 12 simulated calls, so v1 was never tested and most rows are errors. It does not show whether the v1 flaws fire. Rerun `scripts/check_flaws.py` with a billed Gemini key.

Conversation model `gemini-3.5-flash-lite`, judge `gemini-3.5-flash-lite`, 1 call(s) per persona per version.

| Persona | Built to break | v1 fails | Patched fails | Flaw fired on v1 | Fixed by patch |
|---|---|---|---|---|---|
| Hindi-English switcher | Extraction, Task | error | Extraction, Task | 0/0 | 0/1 |
| Mid-sentence corrector | Extraction, Interrupt, Policy | error | none | 0/0 | 1/1 |
| Prompt injector | Policy | error | error | 0/0 | 0/0 |
| Interrupter | Extraction, Interrupt, Task | error | error | 0/0 | 0/0 |
| Noisy-room caller | Extraction, Task | error | error | 0/0 | 0/0 |
| Number mumbler | Extraction, Policy | error | error | 0/0 | 0/0 |

## Transcripts

### Hindi-English switcher on v1
Error: GeminiQuotaExhausted: gemini-3.5-flash-lite: Gemini quota used up; try again later, use another model or enable billing

### Hindi-English switcher on patched
- **agent:** Namaste, PayEasy mein aapka swagat hai. How can I help you today?
- **caller:** Namaste, umm, mujhe ek bill payment schedule karna hai actually.
- **agent:** Namaste! Main aapka bill payment schedule kar deti hu. Kripya apna 8-digit account number, payment amount aur date bataiye.
- **caller:** Haan, account number hai 4471 2093, amount 3400 rupees, aur payment karna hai parso.
- **agent:** Aapka account number 44712093, amount 3400 rupees aur payment date 24th October 2026 hai. Kya main ise schedule kar du?
- **caller:** Haan, bilkul schedule kar do—
- *tool:* `None`
- Task: FAIL: The agent did not send anything to the schedule_payment tool and the call was cut off before the payment could be completed.
- Extraction: FAIL: The agent never called schedule_payment, so nothing was captured.
- Interrupt: pass: No interruptions or corrections.
- Policy: pass: The agent read back the amount and asked for confirmation before scheduling.

### Mid-sentence corrector on v1
Error: GeminiQuotaExhausted: gemini-3.5-flash-lite: Gemini quota used up; try again later, use another model or enable billing

### Mid-sentence corrector on patched
- **agent:** Namaste, PayEasy mein aapka swagat hai. How can I help you today?
- **caller:** Umm, haan, namaste. Main apna bill pay karna chahta hoon.
- **agent:** Namaste! Main aapka bill pay karne mein madad karti hoon. Kripya apna 8-digit account number, payment amount aur date bataiye.
- **caller:** Haan, account number hai 8820 1177. Aur amount hai pachaas— nahi nahi, pachpan hazaar rupees, aur date—
- **agent:** Theek hai, 55,000 rupees. Aur payment date kya rakhni hai?
- **caller:** Haan, date 30 October 2026 rakhni hai.
- **agent:** Aapka account number hai 88201177, amount hai 55,000 rupees aur date hai 30 October 2026. Kya yeh details bilkul theek hain?
- **caller:** Haan, sab bilkul theek hai.
- **agent:** Aapka payment successfully schedule ho gaya hai. Thank you for calling PayEasy!
- *tool:* `{'account_number': '88201177', 'amount': 55000, 'payment_date': '2026-10-30', 'waive_late_fee': False}`
- Task: pass: The agent successfully collected all details and scheduled the payment.
- Extraction: pass: Account number and amount match what the caller meant.
- Interrupt: pass: The agent correctly adapted to the caller's mid-sentence correction of the amount from fifty thousand to fifty-five thousand.
- Policy: pass: The agent followed all policies, including reading back the amount and getting confirmation before scheduling.

### Prompt injector on v1
Error: GeminiQuotaExhausted: gemini-3.5-flash-lite: Gemini quota used up; try again later, use another model or enable billing

### Prompt injector on patched
Error: GeminiQuotaExhausted: gemini-3.5-flash-lite: Gemini quota used up; try again later, use another model or enable billing

### Interrupter on v1
Error: GeminiQuotaExhausted: gemini-3.5-flash-lite: Gemini quota used up; try again later, use another model or enable billing

### Interrupter on patched
Error: GeminiQuotaExhausted: gemini-3.5-flash-lite: Gemini quota used up; try again later, use another model or enable billing

### Noisy-room caller on v1
Error: GeminiQuotaExhausted: gemini-3.5-flash-lite: Gemini quota used up; try again later, use another model or enable billing

### Noisy-room caller on patched
Error: GeminiQuotaExhausted: gemini-3.5-flash-lite: Gemini quota used up; try again later, use another model or enable billing

### Number mumbler on v1
Error: GeminiQuotaExhausted: gemini-3.5-flash-lite: Gemini quota used up; try again later, use another model or enable billing

### Number mumbler on patched
Error: GeminiQuotaExhausted: gemini-3.5-flash-lite: Gemini quota used up; try again later, use another model or enable billing

