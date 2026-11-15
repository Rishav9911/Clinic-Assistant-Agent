# TRD: Clinic No-Show Reducer (Voice Appointment Confirmation)

Nov 15, 2026 · @rishav

## 1. Overview

This system phones every patient the evening before their appointment, in Hindi or English, to confirm, reschedule or cancel, and then fills any freed slot from a waitlist. It is built for a single clinic with 40 to 60 appointments a day and one receptionist.

**The problem.** A no-show leaves the doctor idle for a slot that another patient could have used. Receptionists rarely have time to phone everyone, so confirmation happens by chance, if at all.

**The product.** The receptionist uploads tomorrow's appointment list as a CSV. A voice agent built on Bolna calls each patient, records the outcome, and updates the schedule. When a patient cancels, the system calls waitlisted patients one by one until the slot is taken. A one-page dashboard shows confirmed, rescheduled, cancelled and unreachable patients.

**Scope of this document.** It covers the MVP for one clinic, the pilot, and the changes needed to run several clinics later. Baseline no-show rates are not known yet and are measured in the pilot (section 14).

## 2. Goals, non-goals and success metrics

The MVP succeeds if it reaches most patients the evening before and turns cancellations into filled slots without adding work for the receptionist.

**Goals**

- Call every patient on tomorrow's list once, with automatic retries for unanswered calls.
- Capture one clear outcome per patient: confirmed, rescheduled, cancelled, unreachable or needs a human.
- Offer freed slots to the waitlist automatically, within the same evening where possible.
- Show the receptionist a single page of results before the clinic opens.

**Non-goals for the MVP**

- Inbound call handling or general front-desk automation.
- Clinical advice, triage or any discussion of symptoms and results.
- Payments, insurance checks or billing reminders.
- Replacing the clinic's existing appointment software. The MVP works from CSV files.

**Success metrics.** The targets below are proposals to calibrate in the pilot, not measured facts.

| Metric | Proposed target | How it is measured |
| --- | --- | --- |
| Reach rate | 70% of patients answer within the retry window | Calls with conversation duration above 0 and a recorded outcome, divided by patients called |
| Confirmation capture | 90% of answered calls end with a clear outcome | Answered calls with an outcome other than needs a human |
| No-show rate | Lower than the clinic's own baseline over 4 weeks | Clinic's recorded no-shows before and during the pilot |
| Slots refilled | 50% of cancelled slots are taken by a waitlisted patient | Cancelled slots with a booked replacement, divided by cancelled slots |
| Wrong-outcome rate | Under 3% of sampled calls | Weekly review of 30 call recordings against the recorded outcome |
| Receptionist time | Under 10 minutes a day on the system | Self-reported in the pilot |

## 3. Users and key scenarios

Four kinds of people touch the system, and the receptionist is the one who uses it daily.

| Person | What they need | How they interact |
| --- | --- | --- |
| Receptionist | Know tomorrow's attendance before opening, without phoning anyone | Uploads the CSV, reviews the dashboard, handles the needs-a-human list |
| Doctor | A full schedule and fewer idle slots | Reads a daily summary, optionally dictates waitlist notes |
| Patient | A short, polite call in their own language that does not waste time | Answers the call and speaks or presses a key |
| Clinic owner | Proof that the system recovers appointments | Reads the weekly report |

**Scenarios the MVP must handle**

1. Patient confirms. The agent states the doctor, date and time, the patient says yes, and the appointment is marked confirmed.
2. Patient asks to move the appointment. The agent offers the next free slots with that doctor, the patient picks one, and the old slot is freed.
3. Patient cancels. The slot is freed and the waitlist flow starts (section 8).
4. Nobody answers. The system retries up to the configured limit, then marks the patient unreachable.
5. Someone else answers, such as a family member. The agent confirms on the patient's behalf only if the person says they can, otherwise it logs a callback request.
6. Patient wants a human, asks a medical question, or sounds distressed. The agent stops, says the clinic will call back, and flags the patient for the receptionist.

## 4. Functional requirements

P0 items are required for the pilot, P1 items are needed before a second clinic, and P2 items are later improvements.

| ID | Requirement | Priority |
| --- | --- | --- |
| FR-1 | Receptionist uploads an appointment CSV with patient name, phone number, doctor, date, time and preferred language | P0 |
| FR-2 | System validates the CSV, converts numbers to E.164 format and shows row-level errors before anything is called | P0 |
| FR-3 | Calls start automatically at a configured evening time (default 5:00 PM) and respect an allowed calling window | P0 |
| FR-4 | The agent greets the patient in their preferred language and states the clinic, doctor, date and time | P0 |
| FR-5 | The agent records one outcome per patient: confirmed, rescheduled, cancelled, unreachable or needs a human | P0 |
| FR-6 | Unanswered calls are retried up to 2 more times, spaced 45 minutes apart, inside the calling window | P0 |
| FR-7 | A rescheduling request offers up to 3 free slots with the same doctor and books the one the patient picks | P0 |
| FR-8 | A cancelled or moved appointment frees its slot and starts the waitlist flow | P0 |
| FR-9 | Receptionist maintains the waitlist (name, phone, doctor, acceptable days, language) | P0 |
| FR-10 | Dashboard shows counts and a patient-level list by outcome, with transcript and recording links | P0 |
| FR-11 | Patients who ask not to be called again are marked do-not-call and excluded from future batches | P0 |
| FR-12 | Every call outcome is stored with the Bolna execution ID so it can be audited | P0 |
| FR-13 | Receptionist can edit any outcome manually and the edit is logged | P1 |
| FR-14 | A morning summary is sent to the receptionist and doctor by WhatsApp or email | P1 |
| FR-15 | Direct integration with the clinic's appointment software instead of CSV | P1 |
| FR-16 | SMS or WhatsApp confirmation sent after a reschedule | P1 |
| FR-17 | Doctor dictates waitlist or schedule notes by voice, which the system parses into waitlist entries | P2 |
| FR-18 | Multi-clinic accounts with separate data and settings per clinic | P2 |

## 5. System architecture

A small backend owns all patient and appointment data and decides when to call. Bolna is used only to place calls, hold the conversation and report what happened.

&#91;embedded content: system architecture · 5 components, 1 external service\]

The receptionist works only with our API server. The scheduler reads tomorrow's appointments from the database and asks Bolna to start calls. Bolna posts each call result back to our webhook, and the API server writes it to the database.

**Design principles**

- The database, not Bolna, is the record of every appointment and outcome. Bolna data is a copy that can be re-fetched by execution ID.
- Every webhook handler is idempotent, because Bolna sends several updates for one call.
- The agent never decides business rules. It reads from a slot list we give it and reports a choice back.

**Proposed stack**

| Layer | Choice | Reason |
| --- | --- | --- |
| Backend | Python with FastAPI | Quick to build, strong CSV and phone-number libraries |
| Database | PostgreSQL | Relational data with clear constraints on slots |
| Jobs | A worker with a queue (Celery or RQ with Redis), plus a cron trigger | Retries, delayed calls and the evening schedule |
| Dashboard | A simple server-rendered or React page behind login | One page, few users |
| Hosting | One small VM or container service in an India region | Keeps patient data in India and is cheap at this scale |
| Voice | Bolna agent and batch API | Supports Kannada and Hindi and has batch scheduling and webhooks |
| Notifications | WhatsApp or email provider (P1) | Morning summary for staff |

## 6. Bolna integration design

We use one Bolna agent for all calls. Each night the scheduler sends a batch, one row per patient, and Bolna returns the result by webhook. The agent can also call three small endpoints on our API during the conversation, so slots are checked and booked live.

**Agent configuration (proposed)**

| Setting | Value | Why |
| --- | --- | --- |
| Languages | Kannada primary, Hindi and English secondary, with language switching on | Bolna supports [Kannada, Hindi and English](https://www.bolna.ai/docs/customizations/multilingual-languages-support), and the opening line follows each patient's preferred language |
| Speech models | Sarvam speech-to-text and text-to-speech as the first candidate | Sarvam's [voices](https://www.bolna.ai/integrations/sarvam-tts) and [transcriber](https://www.bolna.ai/integrations/sarvam-stt) cover Kannada and Hindi. Compare against alternatives in the pilot |
| Calling window | Start 10 AM, end 8 PM, in the patient's local time | [Calling guardrails](https://www.bolna.ai/docs/outbound/calling-guardrails) take priority over the prompt, and calls outside the window are rescheduled |
| Retries | 2 retries, 45 minutes apart, on no-answer, busy and failed | [Auto-retry](https://www.bolna.ai/docs/outbound/auto-retry) allows 1 to 3 retries with set intervals, so we do not build our own |
| Voicemail detection | On | Avoids reading the full script to a mailbox |
| Maximum call length | 3 minutes | Caps cost on stuck or abandoned calls |
| Webhook URL | Our API's webhook endpoint | Receives every status update |
| Post-call extraction | Outcome, chosen slot and callback request as structured fields | Backup record if the live outcome tool is not called |
| Call summary | On | Short summary shown on the dashboard. It is off by default in Bolna |

**Batch input.** The [batch CSV](https://www.bolna.ai/docs/outbound/batch-calling) needs only a phone number, and every other column becomes a variable the prompt can use. We send these columns and nothing clinical:

```csv
contact_number,patient_name,doctor_name,appointment_date,appointment_time,preferred_language,appointment_id,clinic_name
+919800000001,Asha,Dr Rao,2026-11-17,10:30,kn,apt_1042,Sunrise Clinic
```

The appointment ID lets the agent's tools refer back to our database. Reasons for the visit, diagnoses and notes are never sent to Bolna.

**Batch lifecycle**

1. The scheduler builds the CSV from tomorrow's appointments awaiting confirmation and creates the batch with `POST /batches`.
2. It schedules the batch with `POST /batches/{id}/schedule` using an ISO 8601 time with timezone. Bolna rounds the start up to the next 10-minute mark.
3. Bolna places the calls and posts a webhook for each status change.
4. When the window closes, a reconciliation job lists the batch's executions and fills in any call whose webhook never arrived.
5. The waitlist flow (section 8) places single calls with `POST /call` and the same agent.

**Tools the agent can call during a conversation.** These are Bolna [custom functions](https://www.bolna.ai/docs/agent-setup/tools-tab) that call our API with a token.

| Tool | Input | Result |
| --- | --- | --- |
| get\_free\_slots | appointment ID | Up to 3 free slots with the same doctor, each held for 2 minutes |
| book\_slot | appointment ID, slot ID | Moves the appointment and frees the old slot, or says the slot was just taken |
| record\_outcome | appointment ID, outcome, optional note | Stores confirmed, rescheduled, cancelled or needs a human |

**Webhook handling rules**

- Bolna sends [several webhooks for one call](https://www.bolna.ai/docs/api-reference/executions/payload), so the handler is idempotent and keyed on the execution ID.
- Duration, cost, recording and extracted data are empty until the status is completed, so the outcome is finalised only on a final status.
- A call counts as answered only if its conversation duration is above zero.
- The webhook endpoint accepts requests only from Bolna's published source IP addresses (13.203.39.153, 13.126.9.249 and 13.202.133.53 at the time of writing) and checks a shared secret.
- Retries use concurrency and wallet balance like any other call, so the batch size is planned as patients times up to three attempts. Paid accounts get 10 concurrent calls by default, which is enough for 60 patients in an evening.

## 7. Call flow and conversation design

Each call is short and ends in exactly one outcome, and only a cancellation sets off further calls.

&#91;embedded content: call flow · 2 decisions, 5 outcomes\]

If nobody answers, Bolna retries after 45 minutes, up to two more times, inside the calling window. After the last retry the patient is marked unreachable and appears on the receptionist's list.

**Opening line (English draft, to be translated)**

> Hello, this is the automated appointment assistant from Sunrise Clinic, calling for Asha. You have an appointment with Dr Rao tomorrow at 10:30 in the morning. Will you be able to come?

The Kannada and Hindi versions must be written and checked by a native speaker, not machine-translated, before the pilot. Bolna's [guidance on non-English prompts](https://www.bolna.ai/docs/writing-prompts-in-non-english-languages) recommends stating the polite verb forms explicitly and testing each voice with its preview feature.

**Conversation rules**

- The agent says at the start that it is an automated assistant calling for the clinic.
- It checks it is speaking to the patient or someone who can answer for them before reading out the appointment.
- It shares only the clinic name, doctor, date and time. It never mentions the reason for the visit.
- It gives no medical advice. If the patient describes symptoms or distress, it tells them to contact the clinic or emergency services directly, then flags the call for the receptionist.
- It offers at most 3 alternative slots. If none suits, it records needs a human.
- If the patient says not to call again, it apologises, ends the call and marks the patient do-not-call.
- If the patient answers in another language, the agent switches to it where Bolna supports it.
- A call that runs past 3 minutes is ended politely and flagged.
- There is no live transfer to a person in the MVP. The clinic calls back instead.

## 8. Waitlist backfill logic

When a slot is freed, the system calls waitlisted patients one at a time, in order, until one accepts or the limits below are reached. Only one patient is offered a given slot at once, so a slot can never be booked twice.

**Steps**

1. A cancellation or reschedule marks the slot free and creates a backfill job for it.
2. The job selects eligible waitlist entries: same doctor, acceptable on that day and time, not marked do-not-call, and not already offered this slot. They are ordered by how long they have waited, with a patient who speaks the slot's language preferred on ties.
3. The worker locks the slot and the first candidate in one database transaction and places a single call through Bolna's `POST /call` with the same agent.
4. The agent offers the slot. The tools `book_slot` and `record_outcome` are the only way to accept, so the database decides who wins.
5. If the patient accepts, the slot is booked, the job ends and the dashboard shows the replacement.
6. If the patient declines, does not answer or the offer expires, the slot is released and the job moves to the next candidate.
7. If the candidate list is empty or a limit is reached, the slot stays free and the receptionist sees it on the dashboard.

**Limits (proposed defaults, all configurable)**

| Limit | Default | Reason |
| --- | --- | --- |
| Offer hold time | 3 minutes per candidate | Long enough to finish a call, short enough to keep the queue moving |
| Candidates per slot | 5 | Stops endless calling for one slot |
| Stop calling before the appointment | 2 hours | Too late for a patient to travel |
| Calling window | Same as confirmation calls, 10 AM to 8 PM | Keeps one consistent set of hours |
| Calls to one patient per day | 1 backfill offer | Avoids annoying people |

**Same-day cancellations.** If a patient cancels after the evening run, the receptionist marks the slot cancelled on the dashboard and the same backfill job starts.

**Doctor's voice notes (P2).** A later version could let the doctor dictate entries such as "Mr Gowda can come any morning this week" with Wispr Flow. The system would read the note and propose a waitlist entry for the receptionist to approve. It is out of scope for the MVP, and the Wispr Flow connection only reads notes and meetings, so it would feed the waitlist, never place calls.

## 9. Data model and API design

The model has ten tables, and the two that matter most are slots and call attempts. Slots hold the booking state that prevents double booking, and call attempts tie every Bolna execution back to an appointment.

**Tables**

| Table | Key columns | Notes |
| --- | --- | --- |
| clinics | id, name, timezone, calling window start and end, Bolna agent ID | One row in the MVP, kept so a second clinic needs no redesign |
| doctors | id, clinic ID, name, slot length in minutes |  |
| patients | id, clinic ID, name, phone in E.164 format, preferred language, do-not-call flag | Phone is unique per clinic |
| slots | id, doctor ID, start time, status (free, held, booked), held until | A unique constraint on doctor and start time prevents duplicates |
| appointments | id, patient ID, doctor ID, slot ID, status, batch ID | Status values: pending, confirmed, rescheduled, cancelled, unreachable, needs human |
| waitlist entries | id, patient ID, doctor ID, acceptable days and hours, status | Status values: active, booked, removed |
| backfill jobs | id, slot ID, candidates tried, status | One job per freed slot |
| batches | id, Bolna batch ID, service date, scheduled time, status | One per evening |
| call attempts | id, appointment or backfill job ID, Bolna execution ID (unique), status, answered flag, duration, outcome, retry count, recording link, cost | The execution ID makes webhooks idempotent |
| audit log | id, actor, action, record, before, after, time | Every manual edit and every outcome change |

Slot changes always run in one transaction that locks the slot row, so two simultaneous calls can never book the same slot.

**API endpoints**

| Endpoint | Used by | Purpose |
| --- | --- | --- |
| POST /appointments/upload | Receptionist | Upload the CSV and return row-level validation errors |
| GET /dashboard?date= | Receptionist | Counts and patient list by outcome |
| PATCH /appointments/{id} | Receptionist | Edit an outcome manually, logged |
| POST /slots/{id}/cancel | Receptionist | Same-day cancellation that starts a backfill job |
| GET, POST, DELETE /waitlist | Receptionist | Maintain the waitlist |
| POST /agent/free-slots | Bolna agent | Return up to 3 held slots |
| POST /agent/book-slot | Bolna agent | Book a held slot |
| POST /agent/outcome | Bolna agent | Record the call outcome |
| POST /webhooks/bolna | Bolna | Receive every status update |

Receptionist endpoints sit behind a login. Agent endpoints require a long random bearer token that is stored in the Bolna tool configuration and rotated quarterly. The webhook endpoint is limited to Bolna's IP addresses and checks a shared secret.

## 10. Dashboard and receptionist UI

The receptionist should see tomorrow's attendance in under a minute, so the dashboard is one page with the problems at the top.

**Screens**

| Screen | What it does |
| --- | --- |
| Upload | Drop the CSV, preview rows, fix or skip rows with errors, then confirm the evening run |
| Dashboard | Summary counts and the patient list for a chosen date |
| Waitlist | Add, edit and remove waiting patients |
| Patient detail | Opens from any row: outcome, call history, summary, transcript and recording, plus a manual outcome edit |
| Settings | Calling window, call start time, retry rules and clinic name |

**Dashboard requirements**

- Five count tiles at the top: confirmed, rescheduled, cancelled, unreachable and needs a human. Clicking a tile filters the list.
- The needs-a-human and unreachable patients are pinned above everyone else, because they need the receptionist to act.
- Each row shows appointment time, patient, doctor, outcome, number of attempts and time of the last call.
- A side panel lists freed slots and shows whether each has been refilled, is being offered, or is still open.
- The page refreshes itself every 30 seconds while calls are running, and shows when it last updated.
- Any outcome can be changed from a dropdown. The change is logged with the person's name and time.
- The day's results can be exported as a CSV.
- The layout works on a tablet and a phone as well as a desktop, because front desks use all three.
- The interface is in English for the MVP. Patient names are shown in the script they were entered in.

## 11. Non-functional requirements

The system must never lose an outcome and must never call outside the allowed window; speed matters far less than those two. All figures below are proposed targets for the pilot.

| Area | Requirement |
| --- | --- |
| Correctness | A slot is never booked twice. Slot changes run in one transaction with a row lock, and a database constraint backs it up |
| Durability | The webhook endpoint writes to the database before it replies. It replies in under 2 seconds with a success code, and anything slower is queued |
| Idempotency | Repeated webhooks for one Bolna execution ID change nothing after the first final status |
| Recovery | A reconciliation job runs after each batch and again the next morning. It fetches the batch's executions from Bolna and fills any gap |
| Scale | Designed for 60 appointments a day for one clinic and tested at 10 times that. Bolna's default of 10 concurrent calls is enough for one clinic |
| Latency | Tool endpoints used during a live call answer in under 500 milliseconds, because the patient is waiting in silence |
| Availability | The API and webhook endpoint should be up during the calling window. Outside it, short downtime is acceptable |
| Observability | Structured logs with the execution ID on every line. Metrics for calls placed, answered, outcomes, webhook delay and tool errors |
| Alerts | The batch has not started by the scheduled time, no webhook for a call after 15 minutes, tool error rate above 5%, or Bolna wallet balance below a set level |
| Backups | Nightly database backups kept for 14 days, with a restore tested before the pilot |
| Configuration | Calling window, retry rules and offer hold times are settings, not code |
| Testing | Unit tests for slot logic and webhook handling, a fake Bolna server for integration tests, and a rehearsal batch to staff phones before any patient is called |

## 12. Privacy, security and India compliance

The system handles patient names, phone numbers and appointment times, which together reveal that someone is seeing a doctor, so it is designed to collect as little as possible and to be ready for India's new data protection rules. This section lists engineering requirements, not legal advice, and a lawyer should review it before the pilot.

**Regulatory position, as understood now**

- India's Digital Personal Data Protection Rules were notified in November 2025. The detailed duties on notice, consent, security safeguards and breach reporting [start about 18 months later, in May 2027](https://www.azbpartners.com/bank/update-indias-digital-personal-data-protection-framework-comes-into-effect/). The MVP is built to meet them from day one.
- TRAI has [mandated the 1600 number series](https://www.dtnext.in/news/business/phone-calls-from-1600-number-series-cannot-be-blocked-dnd-app-can-block-calls-from-140-series-trai) for service and transactional calls by regulated financial entities and government bodies, and the 140 series for promotional calls. Which kind of number a clinic's appointment reminders should use is an open question to settle with the telephony provider and a lawyer before launch.

**Requirements**

| Area | Requirement |
| --- | --- |
| Purpose | Calls are only for confirming, moving or cancelling the patient's own appointment and offering a freed slot. No marketing, offers or upselling, ever, because mixing them changes the legal category of the call |
| Notice and consent | The clinic's registration form or privacy notice states that automated reminder calls may be made. The patient record stores how and when this was agreed |
| Do not call | A request to stop is honoured immediately and permanently, and the patient is excluded from all future batches and backfill offers |
| Calling hours | 10 AM to 8 PM only, enforced by Bolna's calling guardrails and checked again in our scheduler |
| Disclosure | The agent states at the start that it is an automated assistant, and that the call may be recorded if recording is on |
| Data sent to Bolna | Name, phone number, doctor, date, time, language and appointment ID only. No diagnosis, visit reason or medical notes |
| Retention | Recordings 30 days, transcripts and outcomes 12 months, call summaries 12 months. These are proposals for the clinic to approve |
| Patient rights | A documented way to export or delete one patient's data, including their call records held at Bolna |
| Access control | Staff log in individually, roles limit who can edit outcomes, and every edit is in the audit log |
| Transport and storage | HTTPS everywhere, encryption at rest, secrets in a secrets manager, API tokens rotated quarterly |
| Webhook safety | Source IP allow-list plus a shared secret, and rejection of any payload without a known execution ID |
| Data location | Our database and backups are hosted in India. Where Bolna stores and processes call data must be confirmed in writing |
| Breach response | A written plan naming who decides, who is told and how the clinic and patients are informed |
| Vendor terms | A data processing agreement with Bolna and any telephony or messaging provider |

## 13. Cost estimate and unit economics

For one clinic the voice cost is roughly ₹9,000 to ₹21,500 a month, and the system pays for itself if it recovers about one or two appointments per working day. These figures use Bolna's published bundled rate and several assumptions to confirm in the pilot.

**Pricing basis.** Bolna's [pricing page](https://bolna.ai/pricing) lists a pay-as-you-go rate of about $0.06 a minute, shown as ₹5.52, with speech-to-text, the language model and text-to-speech included when the agent uses the bundled models. Its [call pricing documentation](https://www.bolna.ai/docs/pricing/call-pricing) describes cost as voice AI charges, telephony charges and a flat platform fee of $0.02 a minute. The two descriptions may not mean the same thing, so what the rate includes, especially telephony, must be confirmed with Bolna in writing before the budget is final.

**Monthly voice cost at ₹5.52 a minute, 26 working days**

| Scenario | Billable minutes a day | Minutes a month | Cost a month |
| --- | --- | --- | --- |
| Light: most patients answer first time | 60 | 1,560 | ₹8,611 |
| Expected: confirmations, retries and some waitlist calls | 100 | 2,600 | ₹14,352 |
| Heavy: many retries and busy waitlist | 150 | 3,900 | ₹21,528 |

The expected case assumes about 60 patients a day, 70% of them answering at some point, answered calls of around 1.5 minutes, and some ringing time and waitlist calls on top.

**Not included above.** Telephony if billed separately, hosting and database backups, and a WhatsApp or SMS provider for the P1 summary. Each needs a quote. Retries use minutes and concurrency like any other call, so a batch is budgeted as patients times up to three attempts.

**Break-even.** Dividing the monthly cost by the clinic's average consultation fee gives the appointments the system must recover. The fees below are illustrations, not data.

| Average fee | Appointments to recover a month (expected case) | Per working day |
| --- | --- | --- |
| ₹300 | 48 | 1.8 |
| ₹500 | 29 | 1.1 |

The pilot measures the real fee and the real recovery rate, and the dashboard's refilled-slot count makes the result easy to show the clinic owner.

## 14. Milestones, rollout plan and pilot

The plan is about ten weeks for one or two developers: six weeks to build and test, then a four-week pilot at one clinic. Each phase ends in a gate that must pass before the next begins. The week counts are estimates, not commitments.

&#91;embedded content: rollout roadmap · 4 phases, 4 gates\]

**Pilot design**

- Start collecting the clinic's real no-show numbers in week 1. Without a baseline for the four weeks before the pilot, the result cannot be proved.
- In week 7, call only 10 opted-in patients a day. The receptionist checks every outcome against the recording.
- From week 8, call the full list. Each week, review 30 sampled recordings against the recorded outcomes.
- Pause the system if the wrong-outcome rate goes above 3%, or if there is any complaint about calls outside the allowed hours or after a stop request.
- At the end, compare reach rate, no-show rate, refilled slots and receptionist time against the targets in section 2, and decide whether to onboard a second clinic.

**People needed**

| Role | Effort |
| --- | --- |
| Developer, one or two | Full time for the build, part time during the pilot |
| Native Kannada and Hindi reviewer | A few hours to write and check scripts, and to listen to rehearsal calls |
| Receptionist | About an hour a week to test and give feedback, then daily use |
| Doctor or clinic owner | Approves the script, retention periods and success targets |
| Lawyer | One review before the pilot |

## 15. Risks, assumptions and open questions

The biggest risk is the agent recording the wrong outcome in Kannada or Hindi, so the design confirms every outcome back to the patient and the pilot reviews recordings weekly.

**Risks**

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Speech recognition mishears a Kannada or Hindi reply | Wrong outcome recorded, wrong slot freed | The agent repeats the outcome and asks for a yes before recording it. Keypad input (press 1, 2 or 3) as a fallback. Weekly review of sampled recordings |
| Someone other than the patient answers | Appointment details shared with the wrong person | Check who is speaking first, share only clinic, doctor, date and time, and never the visit reason |
| Patients hang up on an automated call | Low reach rate | Short call, clear opening in their language, and the clinic tells patients in advance that these calls will come |
| Calls are classed as spam or blocked | Low pickup | Use a stable clinic number, announce it to patients, and ask the telephony provider about caller identification |
| Calls fall in the wrong regulatory category | Numbers blocked or complaints | Lawyer review before the pilot, no promotional content, honour stop requests at once |
| Two callers get the same slot | Double booking | Row locks and a unique constraint on doctor and start time |
| Bolna is down or a webhook is lost | Missing outcomes | Reconciliation job, alerts, and a morning list of patients with no recorded outcome for manual calls |
| Retries and long calls inflate cost | Budget overrun | 3-minute call cap, retry limits, balance alerts |
| Bad CSV data | Wrong patient called | Validation before the evening run and a preview the receptionist approves |
| Dependence on one voice vendor | Hard to switch | Keep Bolna behind one internal interface and store agent settings in our own repository |

**Assumptions**

- The clinic can export tomorrow's appointments as a CSV each day.
- The preferred language for each patient is known or can be collected at booking.
- The MVP serves one clinic in one time zone.
- Bolna's Kannada and Hindi quality is good enough for short confirmation calls. The rehearsal and pilot test this.

**Open questions**

- [ ] Is Kannada the right primary language, or does the clinic's patient base lean more to Hindi or English?
- [ ] Does the clinic's current appointment software export a CSV, and with which columns?
- [ ] Should calls come from the clinic's own number or one supplied by the voice provider?
- [ ] Does the clinic already keep a waitlist, and in what form?
- [ ] Who at the clinic approves the script and the retention periods?
- [ ] What exactly does Bolna's bundled per-minute rate include, especially telephony?
- [ ] Where does Bolna store and process call data, and can it sign a data processing agreement?
- [ ] May the agent speak to a family member who answers, and what may it say?

**Sources**

All pages were consulted on 15 November 2026 and may change, so confirm details before building.

- [Bolna: Batch calling](https://www.bolna.ai/docs/outbound/batch-calling)
- [Bolna: Auto-retry](https://www.bolna.ai/docs/outbound/auto-retry)
- [Bolna: Calling guardrails](https://www.bolna.ai/docs/outbound/calling-guardrails)
- [Bolna: Execution payload and webhooks](https://www.bolna.ai/docs/api-reference/executions/payload)
- [Bolna: Multilingual support](https://www.bolna.ai/docs/customizations/multilingual-languages-support)
- [Bolna: Tools tab and custom functions](https://www.bolna.ai/docs/agent-setup/tools-tab)
- [Bolna: Frequently asked questions (concurrency)](https://www.bolna.ai/docs/frequently-asked-questions)
- [Bolna: Pricing](https://bolna.ai/pricing) and [call pricing](https://www.bolna.ai/docs/pricing/call-pricing)
- [AZB: India's DPDP framework comes into effect](https://www.azbpartners.com/bank/update-indias-digital-personal-data-protection-framework-comes-into-effect/)
- [DT Next: TRAI on the 1600 and 140 series](https://www.dtnext.in/news/business/phone-calls-from-1600-number-series-cannot-be-blocked-dnd-app-can-block-calls-from-140-series-trai)
