import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams, useSearch } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Badge, Field, Select, Textarea } from "@/components/ui/form";
import { VitalsList } from "@/features/common/VitalsList";
import { useChart } from "@/features/common/queries";
import { BreakGlass } from "@/features/doctor/BreakGlass";
import { Consultation } from "@/features/doctor/Consultation";
import { api, ApiError, call, type Schemas } from "@/lib/api/client";
import { errorMessage, formatDate } from "@/lib/format";

type DoctorChart = Schemas["DoctorChart"];
type ReasonCode = Schemas["AccessReasonIn"]["reason_code"];

export function DoctorPatient() {
  const { t } = useTranslation();
  const { patientId } = useParams({ from: "/app/doctor/patients/$patientId" });
  const { appointment } = useSearch({ from: "/app/doctor/patients/$patientId" });
  const chart = useChart(patientId);
  const [withReason, setWithReason] = useState<DoctorChart | null>(null);

  const needsReason = chart.error instanceof ApiError && chart.error.status === 428;
  const data = withReason ?? (chart.data?.view === "doctor" ? chart.data : null);

  if (needsReason && !data) {
    return (
      <div className="flex flex-col gap-4">
        <ReasonPrompt patientId={patientId} onOpened={setWithReason} />
        <BreakGlass patientId={patientId} />
      </div>
    );
  }
  if (chart.isError && !data) {
    return <Alert tone="error">{errorMessage(chart.error)}</Alert>;
  }
  if (!data) {
    return <p role="status">{t("common.loading")}</p>;
  }

  const p = data.patient;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-baseline gap-2">
        <h1 className="text-xl font-semibold">{p.name}</h1>
        <span className="text-sm text-muted-foreground">
          {p.mrn} · {t(`sex.${p.sex}`)}
          {p.dob && ` · ${formatDate(p.dob)}`}
        </span>
        {data.explanation && <Badge>{t(`templates.${data.explanation}`)}</Badge>}
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle>{t("allergy.title")}</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="flex flex-wrap gap-2">
              {data.allergies.filter((a) => a.is_active).map((a) => (
                <li key={a.id}>
                  <Badge tone="danger">{a.substance}</Badge>
                </li>
              ))}
              {data.allergies.every((a) => !a.is_active) && (
                <li className="text-sm text-muted-foreground">{t("allergy.none")}</li>
              )}
            </ul>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>{t("conditions.title")}</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="text-sm">
              {data.conditions.filter((c) => c.status === "active").map((c) => (
                <li key={c.id}>{c.text}</li>
              ))}
            </ul>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>{t("vitals.latest")}</CardTitle>
          </CardHeader>
          <CardContent>
            <VitalsList vitals={data.vitals.slice(0, 1)} />
          </CardContent>
        </Card>
      </div>
      <Consultation chart={data} appointmentId={appointment} />
    </div>
  );
}

const REASONS: ReasonCode[] = ["covering_doctor", "lab_review", "second_opinion", "administrative", "other"];

function ReasonPrompt({ patientId, onOpened }: { patientId: string; onOpened: (c: DoctorChart) => void }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const [code, setCode] = useState<ReasonCode>("covering_doctor");
  const [text, setText] = useState("");
  const open = useMutation({
    mutationFn: () =>
      call(() =>
        api.POST("/api/v1/patients/{patient_id}/chart", {
          params: { path: { patient_id: patientId } },
          body: { reason_code: code, reason_text: text },
        }),
      ),
    onSuccess: (chart) => {
      client.setQueryData(["chart", patientId], chart);
      onOpened(chart);
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    open.mutate();
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("reason.title")}</CardTitle>
        <p className="text-sm text-muted-foreground">{t("reason.explain")}</p>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <Field label={t("reason.code")} htmlFor="reason-code">
            <Select id="reason-code" value={code} onChange={(e) => setCode(e.target.value as ReasonCode)}>
              {REASONS.map((r) => (
                <option key={r} value={r}>
                  {t(`reason.codes.${r}`)}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={t("reason.text")} htmlFor="reason-text">
            <Textarea id="reason-text" required minLength={5} value={text} onChange={(e) => setText(e.target.value)} />
          </Field>
          {open.isError && <Alert tone="error">{errorMessage(open.error)}</Alert>}
          <Button type="submit" disabled={open.isPending || text.trim().length < 5}>
            {t("reason.open")}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
