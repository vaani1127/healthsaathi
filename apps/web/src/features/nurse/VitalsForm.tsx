import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Alert, Field, Input } from "@/components/ui/form";
import { api, ApiError, call } from "@/lib/api/client";
import { deleteDraft, listDrafts, saveDraft } from "@/lib/drafts";
import { errorMessage } from "@/lib/format";

const FIELDS = ["bp_sys", "bp_dia", "pulse", "temp_c", "spo2", "rr", "weight_kg", "height_cm"] as const;
type VitalsField = (typeof FIELDS)[number];

function toBody(values: Record<VitalsField, string>) {
  const body: Record<string, number | string | null> = {};
  for (const key of FIELDS) {
    const v = values[key].trim();
    if (v) {
      body[key] = key === "temp_c" || key === "weight_kg" || key === "height_cm" ? v : Number(v);
    }
  }
  return body;
}

async function send(patientId: string, body: Record<string, unknown>) {
  return call(() =>
    api.POST("/api/v1/patients/{patient_id}/vitals", {
      params: { path: { patient_id: patientId } },
      body: body as never,
    }),
  );
}

export function VitalsForm({ patientId }: { patientId: string }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const empty = Object.fromEntries(FIELDS.map((f) => [f, ""])) as Record<VitalsField, string>;
  const [values, setValues] = useState(empty);
  const [notice, setNotice] = useState<string | null>(null);

  const drafts = useQuery({
    queryKey: ["drafts", patientId],
    queryFn: async () => (await listDrafts()).filter((d) => d.patientId === patientId),
  });

  const save = useMutation({
    mutationFn: async () => {
      const body = toBody(values);
      try {
        await send(patientId, body);
        return "sent" as const;
      } catch (error) {
        if (error instanceof ApiError && error.status === 0) {
          await saveDraft({
            id: crypto.randomUUID(),
            patientId,
            values: body,
            createdAt: new Date().toISOString(),
          });
          return "draft" as const;
        }
        throw error;
      }
    },
    onSuccess: async (result) => {
      setValues(empty);
      setNotice(result === "sent" ? t("vitals.saved") : t("vitals.savedOffline"));
      await client.invalidateQueries({ queryKey: ["chart", patientId] });
      await client.invalidateQueries({ queryKey: ["drafts", patientId] });
    },
  });

  const flush = useMutation({
    mutationFn: async () => {
      for (const draft of drafts.data ?? []) {
        await send(patientId, draft.values);
        await deleteDraft(draft.id);
      }
    },
    onSettled: async () => {
      await client.invalidateQueries({ queryKey: ["drafts", patientId] });
      await client.invalidateQueries({ queryKey: ["chart", patientId] });
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setNotice(null);
    save.mutate();
  };

  return (
    <form onSubmit={submit} className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {FIELDS.map((f) => (
          <Field key={f} label={t(`vitals.${f}`)} htmlFor={`v-${f}`}>
            <Input
              id={`v-${f}`}
              inputMode="decimal"
              value={values[f]}
              onChange={(e) => setValues((v) => ({ ...v, [f]: e.target.value }))}
            />
          </Field>
        ))}
      </div>
      {save.isError && <Alert tone="error">{errorMessage(save.error)}</Alert>}
      {notice && <Alert tone="success">{notice}</Alert>}
      <Button type="submit" disabled={save.isPending}>
        {t("vitals.save")}
      </Button>
      {(drafts.data?.length ?? 0) > 0 && (
        <Alert>
          <p>{t("vitals.drafts", { count: drafts.data?.length ?? 0 })}</p>
          <Button type="button" size="sm" className="mt-2" onClick={() => flush.mutate()} disabled={flush.isPending}>
            {t("vitals.sendDrafts")}
          </Button>
        </Alert>
      )}
    </form>
  );
}
