# 10 minute demo script

For an evaluator watching the app, run on one laptop with `make demo`. Times are a guide.
Logins, the staff password and authenticator keys come from the table `make demo` prints; the
same table lists the insider cases from the simulation and where each one ended up (alert day
and rank, or the break-glass queue). Use two browser windows (one normal, one private) so a staff
user and a patient can be signed in at the same time. Get any staff code with
`make demo-code EMAIL=...`.

Before you start (1 minute before the audience arrives): run `make demo`, open
http://localhost:8080, and keep the printed table on screen.

## 1. Setting (1 minute)

Say: two small OPD clinics, every role, 50 synthetic patients and about three weeks of activity
from the SaathiBench simulator, a few insiders among the staff. Everything runs here: database,
API, worker, a local blockchain (anvil) and a local git witness.

## 2. A normal visit (3 minutes)

1. Sign in as `reception1.a`. Under "Today's appointments" there are bookings from this
   morning. Search a patient, or "Register new patient": show the consent notice in English, then
   switch to Hindi in the header to show the same notice in Hindi, tick, "Register". Book an
   appointment with `doctor1`, go home, "Issue token".
2. Sign in as `nurse1.a` (second window). Open the token, "Take in", record vitals, "Send to
   doctor".
3. Sign in as `doctor1.a`. Open the patient from "My queue today". Point at the badge next to the
   name: the access is explained by the appointment. "Start consultation", write and sign a note,
   add a prescription with Hindi instructions, sign it and "Print": the bilingual print view.
   Order a CBC.
4. Optional if time: `lab.a` releases the CBC result; the doctor sees it.

## 3. A snooping alert (2 minutes)

1. Sign in as `admin.a`. The "Alerts" tab opens on open alerts of all days.
2. Pick the snooping case from the printed table (for example a reception or nurse login that
   opened a family member's record late in the evening). Open the alert: no workflow reason was
   found, "What stands out" lists the features that made it unusual for this user and clinic,
   and "This user's last accesses" shows the context.
3. Mark it "Misuse" with a note. Say: the detector only ranks accesses for review; access itself
   is decided by the policy and database row security, never by the model.

## 4. A forgery (1 minute)

1. Still as admin, open the forgery case from the table. The access looks explained (an
   appointment or lab order exists), but "Signs of made-up evidence" shows the forgery flags,
   which look at how the evidence was made (for example created by the same user shortly before
   the access, or never followed by a real visit).
2. Say: this is the attack the research targets; explanation-based auditing alone would have
   accepted this access.

## 5. Break-glass (1 minute)

1. As `doctor2.a`, open a patient who is not in their queue: "Reason needed". Use "Break glass",
   choose "Patient unconscious", write a reason, "Open emergency view": allergies, conditions,
   medicines and last vitals, valid for 4 hours.
2. As admin, "Emergency access" tab: the new item is at the top with the reason; seeded
   break-glass uses from earlier days are there too. Mark it "Justified".

## 6. The patient's view and a receipt (1.5 minutes)

1. In the private window open "Patient? Sign in with a code sent by email" and use
   `patient1.a@demo.healthsaathi.test`. Open the dev mailbox link and type the code.
2. "Who opened my record": every access, grouped by day, with who and why in plain words. Switch
   to Hindi to show the same log in Hindi.
3. On an access, "Check receipt": the browser checks the receipt against the clinic's signed
   tree head and reads the local chain directly. All lines say Pass; "Verified".
4. "I do not recognise this" on any line sends a report to the admin ("Patient reports" tab).

## 7. The tamper demo (1 minute)

1. As admin, "Audit log" tab: events, latest checkpoint, "Hash chain intact: Pass", "Log matches
   earlier checkpoints: Pass", and each witness: "Local anvil chain: Confirmed", "Local witness
   folder: Confirmed", "OpenTimestamps: Not configured".
2. In a terminal, change one stored audit event as the database owner (the app's own database
   role cannot do this):

   ```sh
   docker compose -f infra/docker-compose.demo.yml exec postgres psql -U healthsaathi -c \
     "BEGIN; ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only;
      UPDATE audit_events SET payload = payload || '{\"tampered\": true}'
      WHERE seq = (SELECT min(seq) FROM audit_events);
      ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only; COMMIT;"
   ```

3. Press "Refresh" on the audit page: the clinic whose first event was changed shows
   "FAIL: the audit log hash chain is broken". Say: the anchored roots on the chain and in the
   witness folder are outside the database operator's control, so a rewrite is detectable.
4. Undo it so the demo stays usable:

   ```sh
   docker compose -f infra/docker-compose.demo.yml exec postgres psql -U healthsaathi -c \
     "BEGIN; ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only;
      UPDATE audit_events SET payload = payload - 'tampered'
      WHERE seq = (SELECT min(seq) FROM audit_events);
      ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only; COMMIT;"
   ```

   If you are signed in as the admin of the other clinic, use `make demo-reset` and `make demo`
   instead of looking for the right row; the same steps are automated in
   `apps/web/e2e-demo/tamper.spec.ts`.

## Closing line

Every access is explained or reviewed, every review is logged, and the log itself can be checked
by people outside the clinic, including the patient. Designed to support obligations under the
DPDP Act 2023 and DPDP Rules 2025; not legally reviewed or certified.
