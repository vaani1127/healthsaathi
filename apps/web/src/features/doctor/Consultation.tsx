import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Badge, Field, Input, Select, Textarea } from "@/components/ui/form";
import { useDoctors } from "@/features/common/queries";
import { PrescriptionBuilder } from "@/features/doctor/PrescriptionBuilder";
import { api, call, type Schemas } from "@/lib/api/client";
import { errorMessage, formatDateTime, toLocalInput } from "@/lib/format";
import { useAuth } from "@/stores/auth";

type Chart = Schemas["DoctorChart"];

export function Consultation({ chart, appointmentId }: { chart: Chart; appointmentId?: string | undefined }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const patientId = chart.patient.id;
  const [encounter, setEncounter] = useState<Schemas["EncounterOut"] | null>(null);
  const refresh = () => client.invalidateQueries({ queryKey: ["chart", patientId] });

  const start = useMutation({
    mutationFn: () =>
      call(() =>
        api.POST("/api/v1/appointments/{appointment_id}/encounter", {
          params: { path: { appointment_id: appointmentId ?? "" } },
        }),
      ),
    onSuccess: setEncounter,
  });
  const close = useMutation({
    mutationFn: () =>
      call(() =>
        api.POST("/api/v1/encounters/{encounter_id}/close", {
          params: { path: { encounter_id: encounter?.id ?? "" } },
        }),
      ),
    onSuccess: async (e) => {
      setEncounter(e);
      await client.invalidateQueries({ queryKey: ["queue"] });
    },
  });

  return (
    <div className="flex flex-col gap-4">
      {appointmentId && !encounter && (
        <Button onClick={() => start.mutate()} disabled={start.isPending}>
          {t("consult.start")}
        </Button>
      )}
      {start.isError && <Alert tone="error">{errorMessage(start.error)}</Alert>}
      {encounter?.status === "open" && (
        <>
          <NoteEditor patientId={patientId} encounterId={encounter.id} onSaved={refresh} />
          <PrescriptionBuilder patientId={patientId} encounterId={encounter.id} onSaved={refresh} />
          <LabOrderForm patientId={patientId} encounterId={encounter.id} onSaved={refresh} />
          <div className="grid gap-4 md:grid-cols-2">
            <ReferralForm patientId={patientId} />
            <FollowUpForm patientId={patientId} />
          </div>
          <Button variant="outline" onClick={() => close.mutate()} disabled={close.isPending}>
            {t("consult.finish")}
          </Button>
        </>
      )}
      {encounter?.status === "closed" && <Alert tone="success">{t("consult.finished")}</Alert>}
      <History chart={chart} onChanged={refresh} />
    </div>
  );
}

function NoteEditor({
  patientId,
  encounterId,
  onSaved,
}: {
  patientId: string;
  encounterId: string;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const [body, setBody] = useState("");
  const [note, setNote] = useState<Schemas["NoteOut"] | null>(null);

  const save = useMutation({
    mutationFn: () =>
      note
        ? call(() =>
            api.PATCH("/api/v1/notes/{note_id}", { params: { path: { note_id: note.id } }, body: { body } }),
          )
        : call(() =>
            api.POST("/api/v1/patients/{patient_id}/notes", {
              params: { path: { patient_id: patientId } },
              body: { encounter_id: encounterId, body },
            }),
          ),
    onSuccess: (n) => {
      setNote(n);
      onSaved();
    },
  });
  const sign = useMutation({
    mutationFn: () =>
      call(() => api.POST("/api/v1/notes/{note_id}/sign", { params: { path: { note_id: note?.id ?? "" } } })),
    onSuccess: (n) => {
      setNote(n);
      onSaved();
    },
  });

  const signed = Boolean(note?.signed_at);
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("notes.title")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Field label={t("notes.body")} htmlFor="note-body">
          <Textarea id="note-body" rows={6} disabled={signed} value={body} onChange={(e) => setBody(e.target.value)} />
        </Field>
        {(save.isError || sign.isError) && <Alert tone="error">{errorMessage(save.error ?? sign.error)}</Alert>}
        {signed ? (
          <Alert tone="success">{t("notes.signed")}</Alert>
        ) : (
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => save.mutate()} disabled={!body.trim() || save.isPending}>
              {t("notes.saveDraft")}
            </Button>
            <Button onClick={() => sign.mutate()} disabled={!note || sign.isPending || save.isPending}>
              {t("notes.sign")}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

const LAB_TESTS = [
  { code: "CBC", name: "Complete blood count" },
  { code: "RBS", name: "Random blood sugar" },
  { code: "HBA1C", name: "HbA1c" },
  { code: "LFT", name: "Liver function test" },
  { code: "KFT", name: "Kidney function test" },
  { code: "LIPID", name: "Lipid profile" },
  { code: "URINE", name: "Urine routine" },
];

function LabOrderForm({ patientId, encounterId, onSaved }: { patientId: string; encounterId: string; onSaved: () => void }) {
  const { t } = useTranslation();
  const [chosen, setChosen] = useState<string[]>([]);
  const order = useMutation({
    mutationFn: () =>
      call(() =>
        api.POST("/api/v1/patients/{patient_id}/lab-orders", {
          params: { path: { patient_id: patientId } },
          body: { encounter_id: encounterId, tests: LAB_TESTS.filter((x) => chosen.includes(x.code)) },
        }),
      ),
    onSuccess: () => {
      setChosen([]);
      onSaved();
    },
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("lab.order")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {LAB_TESTS.map((test) => (
            <label key={test.code} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                className="size-4"
                checked={chosen.includes(test.code)}
                onChange={(e) =>
                  setChosen((c) => (e.target.checked ? [...c, test.code] : c.filter((x) => x !== test.code)))
                }
              />
              {t(`lab.tests.${test.code}`)}
            </label>
          ))}
        </div>
        {order.isError && <Alert tone="error">{errorMessage(order.error)}</Alert>}
        {order.isSuccess && <Alert tone="success">{t("lab.ordered")}</Alert>}
        <Button onClick={() => order.mutate()} disabled={chosen.length === 0 || order.isPending}>
          {t("lab.placeOrder")}
        </Button>
      </CardContent>
    </Card>
  );
}

function ReferralForm({ patientId }: { patientId: string }) {
  const { t } = useTranslation();
  const me = useAuth((s) => s.me);
  const doctors = useDoctors();
  const [to, setTo] = useState("");
  const [reason, setReason] = useState("");
  const refer = useMutation({
    mutationFn: () =>
      call(() =>
        api.POST("/api/v1/patients/{patient_id}/referrals", {
          params: { path: { patient_id: patientId } },
          body: { to_user_id: to, reason },
        }),
      ),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    refer.mutate();
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("referral.title")}</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <Field label={t("referral.to")} htmlFor="ref-to">
            <Select id="ref-to" required value={to} onChange={(e) => setTo(e.target.value)}>
              <option value="">{t("common.choose")}</option>
              {doctors.data
                ?.filter((d) => d.user_id !== me?.user_id)
                .map((d) => (
                  <option key={d.user_id} value={d.user_id}>
                    {d.name}
                  </option>
                ))}
            </Select>
          </Field>
          <Field label={t("referral.reason")} htmlFor="ref-reason">
            <Textarea id="ref-reason" required minLength={5} value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
          {refer.isError && <Alert tone="error">{errorMessage(refer.error)}</Alert>}
          {refer.isSuccess && <Alert tone="success">{t("referral.sent")}</Alert>}
          <Button type="submit" disabled={refer.isPending}>
            {t("referral.send")}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

function FollowUpForm({ patientId }: { patientId: string }) {
  const { t } = useTranslation();
  const [when, setWhen] = useState(() => toLocalInput(new Date(Date.now() + 14 * 86_400_000)));
  const book = useMutation({
    mutationFn: () =>
      call(() =>
        api.POST("/api/v1/patients/{patient_id}/follow-ups", {
          params: { path: { patient_id: patientId } },
          body: { slot_start: new Date(when).toISOString() },
        }),
      ),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    book.mutate();
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("followUp.title")}</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <Field label={t("reception.when")} htmlFor="fu-when">
            <Input id="fu-when" type="datetime-local" required value={when} onChange={(e) => setWhen(e.target.value)} />
          </Field>
          {book.isError && <Alert tone="error">{errorMessage(book.error)}</Alert>}
          {book.isSuccess && <Alert tone="success">{t("followUp.booked")}</Alert>}
          <Button type="submit" disabled={book.isPending}>
            {t("followUp.book")}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

function History({ chart, onChanged }: { chart: Chart; onChanged: () => void }) {
  const { t } = useTranslation();
  const sign = useMutation({
    mutationFn: (rxId: string) =>
      call(() => api.POST("/api/v1/prescriptions/{rx_id}/sign", { params: { path: { rx_id: rxId } } })),
    onSuccess: onChanged,
  });
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>{t("notes.history")}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 text-sm">
          {chart.notes.length === 0 && <p className="text-muted-foreground">{t("notes.none")}</p>}
          {chart.notes.map((n) => (
            <article key={n.id} className="rounded-md border p-2">
              <p className="text-xs text-muted-foreground">
                {formatDateTime(n.created_at)} · v{n.version}{" "}
                {n.signed_at ? <Badge tone="success">{t("notes.signedBadge")}</Badge> : <Badge>{t("notes.draft")}</Badge>}
              </p>
              <p className="whitespace-pre-wrap">{n.body}</p>
            </article>
          ))}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>{t("rx.history")}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 text-sm">
          {chart.prescriptions.length === 0 && <p className="text-muted-foreground">{t("rx.none")}</p>}
          {chart.prescriptions.map((rx) => (
            <article key={rx.id} className="flex flex-col gap-1 rounded-md border p-2">
              <p className="text-xs text-muted-foreground">
                {formatDateTime(rx.created_at)} · v{rx.version}
              </p>
              <ul>
                {rx.items.map((item, i) => (
                  <li key={i}>
                    {String(item.drug)} {String(item.strength ?? "")} · {String(item.dose)}
                  </li>
                ))}
              </ul>
              {rx.signed_at ? (
                <Link
                  to="/print/prescription/$rxId"
                  params={{ rxId: rx.id }}
                  search={{ patient: chart.patient.id }}
                  className="text-primary underline"
                >
                  {t("rx.print")}
                </Link>
              ) : (
                <Button size="sm" onClick={() => sign.mutate(rx.id)} disabled={sign.isPending}>
                  {t("rx.sign")}
                </Button>
              )}
            </article>
          ))}
          {sign.isError && <Alert tone="error">{errorMessage(sign.error)}</Alert>}
        </CardContent>
      </Card>
    </div>
  );
}
