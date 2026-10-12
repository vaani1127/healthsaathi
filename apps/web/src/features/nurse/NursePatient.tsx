import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams, useSearch } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Field, Input, Select } from "@/components/ui/form";
import { Empty, Loading, LoadError } from "@/components/ui/state";
import { useChart } from "@/features/common/queries";
import { VitalsForm } from "@/features/nurse/VitalsForm";
import { VitalsList } from "@/features/common/VitalsList";
import { api, call, type Schemas } from "@/lib/api/client";
import { errorMessage } from "@/lib/format";

export function NursePatient() {
  const { t } = useTranslation();
  const { patientId } = useParams({ from: "/app/nurse/patients/$patientId" });
  const { token } = useSearch({ from: "/app/nurse/patients/$patientId" });
  const chart = useChart(patientId);
  const data = chart.data?.view === "nurse" ? chart.data : null;

  if (chart.isError) {
    return <LoadError error={chart.error} onRetry={() => void chart.refetch()} />;
  }
  if (!data) {
    return <Loading />;
  }
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold">{data.patient.name}</h1>
        {token && <TokenActions tokenId={token} />}
      </div>
      <Card>
        <CardHeader>
          <CardTitle>{t("vitals.record")}</CardTitle>
        </CardHeader>
        <CardContent>
          <VitalsForm patientId={patientId} />
        </CardContent>
      </Card>
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>{t("vitals.history")}</CardTitle>
          </CardHeader>
          <CardContent>
            <VitalsList vitals={data.vitals} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>{t("allergy.title")}</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <AllergyList patientId={patientId} allergies={data.allergies} />
            <AddAllergy patientId={patientId} />
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function TokenActions({ tokenId }: { tokenId: string }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const move = useMutation({
    mutationFn: (status: Schemas["QueueStatus"]) =>
      call(() =>
        api.PATCH("/api/v1/queue/tokens/{token_id}", { params: { path: { token_id: tokenId } }, body: { status } }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["queue"] }),
  });
  return (
    <div className="flex gap-2">
      <Button size="sm" variant="outline" disabled={move.isPending} onClick={() => move.mutate("with_nurse")}>
        {t("queue.takeIn")}
      </Button>
      <Button size="sm" disabled={move.isPending} onClick={() => move.mutate("with_doctor")}>
        {t("queue.sendToDoctor")}
      </Button>
      {move.isError && <span className="text-sm text-destructive">{errorMessage(move.error)}</span>}
    </div>
  );
}

function AllergyList({ patientId, allergies }: { patientId: string; allergies: Schemas["AllergyOut"][] }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const toggle = useMutation({
    mutationFn: (a: Schemas["AllergyOut"]) =>
      call(() =>
        api.PATCH("/api/v1/allergies/{allergy_id}", {
          params: { path: { allergy_id: a.id } },
          body: { is_active: !a.is_active },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["chart", patientId] }),
  });
  if (allergies.length === 0) {
    return <Empty>{t("allergy.none")}</Empty>;
  }
  return (
    <>
      <ul className="flex flex-col divide-y text-sm">
        {allergies.map((a) => (
          <li key={a.id} className="flex flex-wrap items-center justify-between gap-2 py-1.5">
            <span className={a.is_active ? "" : "text-muted-foreground line-through"}>
              {a.substance} · {t(`severity.${a.severity}`)}
              {a.reaction && ` · ${a.reaction}`}
            </span>
            <Button size="sm" variant="ghost" disabled={toggle.isPending} onClick={() => toggle.mutate(a)}>
              {a.is_active ? t("allergy.deactivate") : t("allergy.reactivate")}
            </Button>
          </li>
        ))}
      </ul>
      {toggle.isError && <Alert tone="error">{errorMessage(toggle.error)}</Alert>}
    </>
  );
}

function AddAllergy({ patientId }: { patientId: string }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const [substance, setSubstance] = useState("");
  const [severity, setSeverity] = useState<Schemas["AllergySeverity"]>("moderate");
  const add = useMutation({
    mutationFn: () =>
      call(() =>
        api.POST("/api/v1/patients/{patient_id}/allergies", {
          params: { path: { patient_id: patientId } },
          body: { substance, severity },
        }),
      ),
    onSuccess: async () => {
      setSubstance("");
      await client.invalidateQueries({ queryKey: ["chart", patientId] });
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    add.mutate();
  };
  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
      <Field label={t("allergy.substance")} htmlFor="allergy-substance" className="flex-1">
        <Input id="allergy-substance" required minLength={2} value={substance} onChange={(e) => setSubstance(e.target.value)} />
      </Field>
      <Field label={t("allergy.severity")} htmlFor="allergy-severity">
        <Select id="allergy-severity" value={severity} onChange={(e) => setSeverity(e.target.value as Schemas["AllergySeverity"])}>
          {(["mild", "moderate", "severe"] as const).map((s) => (
            <option key={s} value={s}>
              {t(`severity.${s}`)}
            </option>
          ))}
        </Select>
      </Field>
      <Button type="submit" size="sm" disabled={add.isPending}>
        {t("allergy.add")}
      </Button>
      {add.isError && <Alert tone="error">{errorMessage(add.error)}</Alert>}
    </form>
  );
}
