# HealthSaathi user guide

One section per role. Button and screen names are the English ones; every screen also works in
Hindi (the "हिन्दी" button in the header switches language, "English" switches back). The app
works on a phone (360 pixels wide or more) and can be installed from the browser menu ("Install
app" or "Add to home screen").

In the local demo (`make demo`) the logins are printed in the terminal. Staff use the same
password; each staff login has its own authenticator key.

## Everyone with a staff account

**Signing in.** Type your clinic email and password, then the 6 digit code from your
authenticator app (Google Authenticator, Microsoft Authenticator or similar). The first time, the
app shows a QR code to scan; staff must use an authenticator before they can see patient data.
If you belong to more than one clinic, choose the clinic after signing in.

**Invited by email.** The invite link opens "Set your password". Choose a password of at least 10
characters, then sign in and set up the authenticator.

**Forgot your password.** On the sign-in page press "Forgot password?", type your clinic email
and press "Send link". The page always says "If this email has an account, we sent a link", so it
does not show which emails have accounts. Open the link in the email within 30 minutes (it works
once; asking again makes older links stop working). Choose a new password, type it again, and if
you use an authenticator app type its current 6 digit code too. After that every device is
signed out; sign in again with the new password and your authenticator code. You get an email
saying your password was changed: if it was not you, tell your clinic admin straight away. If you
lost your authenticator, ask your clinic admin instead. In the local demo the email appears in the
dev mailbox (`/dev/mailbox`).

**My sessions.** Your name in the header (or "My sessions" on a phone) lists every device signed
in to your account. "Sign out this device" ends a session you do not recognise. You also get an
email when your account signs in from a new device.

**Why your access is shown to the patient.** Every time you open a patient's record the app
records who, what, when and the workflow reason it found (appointment, queue token, lab order,
referral, care team, front desk work, break-glass). The patient can see this and verify it.

## Reception

- **Find or register a patient.** Search by name, phone or record number. "Register new patient"
  shows the clinic's consent notice; read it to the patient, tick the box, then "Register".
- **Appointments.** On the patient's page choose the doctor and the date and time, then "Book
  appointment". "Walk-in" adds the patient to today's queue straight away.
- **Today's appointments** (home screen): "Issue token" puts a booked patient in the queue.
  "No-show" and "Cancel" close an appointment.
- **Billing.** On the patient's page, enter quantities next to the services, "Create invoice",
  "Issue invoice", then "Record payment" (cash, UPI or card machine). "Print receipt" opens a
  bilingual receipt; print it or save it as PDF from the browser.
- **Patient portal.** Type the patient's email and "Give portal access"; the patient can then
  sign in with codes sent to that email.

## Nurse

- **Today's queue** lists tokens in order. Open a patient to work.
- **Take in** marks that you are with the patient; **Send to doctor** passes them on.
- **Record vitals.** Fill in what you measured and "Save vitals". Without a connection the
  vitals are kept on this device ("No connection. Saved on this device"); press "Send now" when
  you are back online. Unsent vitals are deleted when you sign out.
- **Allergies.** Add a substance and its severity. "Mark inactive" when an allergy no longer
  applies; it stays in the history.

## Doctor

- **My queue today** lists your patients. Opening a patient from the queue is explained by the
  appointment or token.
- **Opening another patient.** If the app finds no reason, it asks for one ("Reason needed").
  Choose a reason code and write a short reason; this access is recorded with your reason and
  reviewed like any other.
- **Emergency access (break glass).** For an emergency only: choose the reason and describe it,
  then "Open emergency view". You see allergies, active conditions, current medicines and the last
  vitals for 4 hours. Every use goes to the clinic admin's review queue.
- **Consultation.** "Start consultation", then:
  - Note: write, "Save draft", then "Sign note". A signed note cannot change; "Amend" adds a new
    version and "Show versions" lists all of them.
  - Prescription: add medicines, dose (for example 1-0-1), days and instructions in English and
    Hindi, "Save prescription", then "Sign" and "Print". "Amend" on a signed prescription adds a
    new version.
  - Lab tests: tick the tests and "Place order".
  - Referral to another doctor in the clinic, and "Book follow-up".
  - "Finish consultation" when done.
- **Conditions.** Add a condition with an optional start date; "Mark resolved" when it is over.
- **Lab results** appear on the patient's page once the lab releases them; "Print report" opens
  a bilingual lab report.
- **Referrals and care team.** The referral list shows open referrals ("Mark completed",
  "Cancel referral"). The care team card adds or ends staff on the patient's care team.

## Lab technician

- **Lab worklist** shows tests ordered for today's patients.
- "Sample collected" when the sample is taken. Then enter each result and its unit, attach the
  report file if there is one (PDF, PNG or JPEG), and "Save and release result". The doctor and
  the patient see it at once.

## Clinic admin

The admin screen has tabs:

- **Alerts.** Accesses the detector found unusual, highest score first, a few per day. "Open
  alerts, all days" is the default; "One day" picks a date. Open an alert to see the patient, the
  reason the app found (or none), what stands out, any forgery flags and the user's last
  accesses. Mark it "Misuse", "Justified" or "Not sure", with a note. The app then shows what
  will be recorded and asks you to confirm with "Record review": a review is written to the
  audit log and cannot be changed later. "Cancel" (or Escape) records nothing. You cannot review
  an alert about yourself.
  - Today's and yesterday's alerts are **Provisional**. Some signs of made-up evidence (the
    appointment never went ahead, or was cancelled after the record was opened) can only be seen
    24 hours later. So two days after a day ends, at midnight clinic time, the app makes that
    day's **Final** list. It can add alerts the provisional list missed and retire provisional
    alerts that did not make the final list. The "One day" view tells you whether the day's list
    is final and, if not, when it will be.
  - An alert you have already reviewed is never changed. If it is not in the final list it is
    shown as "Reviewed, not in final list".
  - Retired alerts are not open for review and are hidden. In "One day", tick "Show retired
    alerts" to see them.
  - If the app was off for longer than its catch-up window (7 days unless the platform team
    set more), older days are not finalised; their alerts stay provisional and the day view
    says so.
- **Staff.** Invite staff by email and role; deactivate or activate memberships. "Deactivate"
  asks you to confirm first: the person cannot sign in to this clinic until you press
  "Activate". "No password yet" marks staff who have not used their invite; "Send invite again"
  emails a new link, and only the newest link works. "No authenticator yet" marks staff who
  have not set one up.
- **Schedules and Shifts.** Doctor schedules (day, hours, slot length) and staff shifts for the
  coming week. "Pause" on a schedule asks you to confirm first; no new appointments can be
  booked in that slot until you press "Activate".
- **Services.** Services and prices used for invoices. "Pause" asks you to confirm first; the
  service is not offered on new invoices until you press "Activate".
- **Consent notices.** Write a new version in English and Hindi, then "Publish". Patients
  registered after that see the new version.
- **Emergency access.** Every break-glass use from the last days, with reason. Mark each one
  "Justified", "Misuse" or "Not sure", then confirm with "Record review": the review cannot be
  changed later.
- **Patient reports.** Accesses patients said they do not recognise. Close them once handled;
  "Close" asks you to confirm with "Close report", because a closed report cannot be reopened
  from this screen.
- **Revenue.** Money collected on a day.
- **Audit log.** Whether the access log is intact: number of events, the latest signed
  checkpoint, "Hash chain intact" and "Log matches earlier checkpoints" (Pass or Fail), and the
  state of each witness the log is published to: the chain, the witness repository and
  OpenTimestamps, each "Confirmed", "Pending", "Failed" or "Not configured". If anyone changes a
  stored event, this page shows FAIL.

## Patient

- **Sign in** at "Patient? Sign in with a code sent by email": type your email and the 6 digit
  code sent to it. No password.
- **My record:** prescriptions, lab results (with "Print report"), vitals, appointments and
  bills.
- **Who opened my record:** every access by clinic staff, grouped by day, with who, what and
  why, for example "Dr X (Doctor) opened your notes because of appointment token 23 at 12:05".
  - "I do not recognise this" sends a note to the clinic admin.
  - "Check receipt" verifies, in your browser, that the access is in the clinic's signed log and
    that the log's fingerprint is on the public chain. "Verified" means every check passed.
  - "Turn on notifications" (on devices that support it) sends a notification each time someone
    at the clinic opens your record. It only says that the record was opened.
- **Consent:** your consent and its history; "Withdraw consent" at any time.
- **Download:** your record and access log as a file, the same record as FHIR for other health
  apps, or "Print my record".
- **Check a receipt someone sent you:** open `/verify`, paste the receipt or choose the file.

FHIR R4 export follows NRCeS ABDM profiles, validated offline. Not ABDM certified. Designed to
support obligations under the DPDP Act 2023 and DPDP Rules 2025. Not legally reviewed or
certified.
