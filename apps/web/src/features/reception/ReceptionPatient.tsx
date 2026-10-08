import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Badge, Field, Input, Select } from "@/components/ui/form";
import { useChart, useDoctors, useQueue } from "@/features/common/queries";
import { api, call } from "@/lib/api/client";
import { errorMessage, formatDate, formatDateTime, toLocalInput } from "@/lib/format";

export function ReceptionPatient() {
  const { t } = useTranslation();
  const { patientId } = useParams({ from: "/app/reception/patients/$patientId" });
  const chart = useChart(patientId);
  const data = chart.data?.view === "reception" ? chart.data : null;

  if (chart.isError) {
    return <Alert tone="error">{errorMessage(chart.error)}</Alert>;
  }
  if (!data) {
    return <p role="status">{t("common.loading")}</p>;
  }
  const p = data.patient;
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>{p.name}</CardTitle>
          <p className="text-sm text-muted-foreground">
            {p.mrn} · {t(`sex.${p.sex}`)}
            {p.dob && ` · ${formatDate(p.dob)}`}
          </p>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 text-sm">
          {p.phone && <p>{p.phone}</p>}
          {p.address && <p>{p.address}</p>}
          <p>
            {t("consent.status")}:{" "}
            {data.consent && !data.consent.withdrawn_at ? (
              <Badge tone="success">{t("consent.given", { version: data.consent.notice_version })}</Badge>
            ) : (
              <Badge tone="warning">{t("consent.missing")}</Badge>
            )}
          </p>
          <h3 className="mt-2 font-semibold">{t("reception.appointments")}</h3>
          <ul className="divide-y">
            {data.appointments.map((a) => (
              <li key={a.id} className="flex justify-between py-1">
                <span>{formatDateTime(a.slot_start)}</span>
                <span className="text-muted-foreground">{t(`appointmentStatus.${a.status}`)}</span>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
      <div className="flex flex-col gap-4">
        <BookAppointment patientId={patientId} />
        <WalkIn patientId={patientId} />
      </div>
    </div>
  );
}

function BookAppointment({ patientId }: { patientId: string }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const doctors = useDoctors();
  const [doctor, setDoctor] = useState("");
  const [when, setWhen] = useState(() => toLocalInput(new Date(Date.now() + 15 * 60_000)));

  const book = useMutation({
    mutationFn: () =>
      call(() =>
        api.POST("/api/v1/appointments", {
          body: { patient_id: patientId, doctor_user_id: doctor, slot_start: new Date(when).toISOString() },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["chart", patientId] }),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    book.mutate();
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("reception.book")}</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <Field label={t("reception.doctor")} htmlFor="book-doctor">
            <Select id="book-doctor" required value={doctor} onChange={(e) => setDoctor(e.target.value)}>
              <option value="">{t("common.choose")}</option>
              {doctors.data?.map((d) => (
                <option key={d.user_id} value={d.user_id}>
                  {d.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={t("reception.when")} htmlFor="book-when">
            <Input id="book-when" type="datetime-local" required value={when} onChange={(e) => setWhen(e.target.value)} />
          </Field>
          {book.isError && <Alert tone="error">{errorMessage(book.error)}</Alert>}
          {book.isSuccess && <Alert tone="success">{t("reception.booked")}</Alert>}
          <Button type="submit" disabled={book.isPending}>
            {t("reception.book")}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

function WalkIn({ patientId }: { patientId: string }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const doctors = useDoctors();
  const queue = useQueue();
  const [doctor, setDoctor] = useState("");

  const walkIn = useMutation({
    mutationFn: () =>
      call(() => api.POST("/api/v1/walkins", { body: { patient_id: patientId, doctor_user_id: doctor } })),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["queue"] });
      await client.invalidateQueries({ queryKey: ["chart", patientId] });
    },
  });
  const token = queue.data?.find((q) => q.patient.id === patientId && q.status !== "done");

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("reception.walkIn")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {token ? (
          <Alert tone="success" data-testid="token-number">
            {t("reception.tokenNo", { no: token.token_no })}
          </Alert>
        ) : (
          <>
            <Field label={t("reception.doctor")} htmlFor="walkin-doctor">
              <Select id="walkin-doctor" value={doctor} onChange={(e) => setDoctor(e.target.value)}>
                <option value="">{t("common.choose")}</option>
                {doctors.data?.map((d) => (
                  <option key={d.user_id} value={d.user_id}>
                    {d.name}
                  </option>
                ))}
              </Select>
            </Field>
            {walkIn.isError && <Alert tone="error">{errorMessage(walkIn.error)}</Alert>}
            <Button disabled={!doctor || walkIn.isPending} onClick={() => walkIn.mutate()}>
              {t("reception.addWalkIn")}
            </Button>
          </>
        )}
      </CardContent>
    </Card>
  );
}
