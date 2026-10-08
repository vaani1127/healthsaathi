import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Field, Input, Textarea } from "@/components/ui/form";
import { api, call, type Schemas } from "@/lib/api/client";
import { errorMessage } from "@/lib/format";

type Item = Schemas["PrescriptionItem"];

const blank = (): Item => ({ drug: "", strength: "", dose: "", duration_days: 5, instructions_en: "", instructions_hi: "" });

export function PrescriptionBuilder({
  patientId,
  encounterId,
  onSaved,
}: {
  patientId: string;
  encounterId: string;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const [items, setItems] = useState<Item[]>([blank()]);
  const [adviceEn, setAdviceEn] = useState("");
  const [adviceHi, setAdviceHi] = useState("");

  const save = useMutation({
    mutationFn: () =>
      call(() =>
        api.POST("/api/v1/patients/{patient_id}/prescriptions", {
          params: { path: { patient_id: patientId } },
          body: {
            encounter_id: encounterId,
            items: items.map((i) => ({
              ...i,
              strength: i.strength || null,
              instructions_en: i.instructions_en || null,
              instructions_hi: i.instructions_hi || null,
            })),
            advice_en: adviceEn || null,
            advice_hi: adviceHi || null,
          },
        }),
      ),
    onSuccess: () => {
      setItems([blank()]);
      setAdviceEn("");
      setAdviceHi("");
      onSaved();
    },
  });

  const update = (index: number, key: keyof Item, value: string) =>
    setItems((list) =>
      list.map((item, i) => (i === index ? { ...item, [key]: key === "duration_days" ? Number(value) : value } : item)),
    );

  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate();
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("rx.title")}</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="flex flex-col gap-3">
          {items.map((item, i) => (
            <fieldset key={i} className="grid grid-cols-2 gap-2 rounded-md border p-2 sm:grid-cols-4">
              <legend className="px-1 text-xs text-muted-foreground">{t("rx.item", { no: i + 1 })}</legend>
              <Field label={t("rx.drug")} htmlFor={`rx-drug-${i}`} className="col-span-2">
                <Input id={`rx-drug-${i}`} required minLength={2} value={item.drug} onChange={(e) => update(i, "drug", e.target.value)} />
              </Field>
              <Field label={t("rx.strength")} htmlFor={`rx-strength-${i}`}>
                <Input id={`rx-strength-${i}`} value={item.strength ?? ""} onChange={(e) => update(i, "strength", e.target.value)} />
              </Field>
              <Field label={t("rx.dose")} htmlFor={`rx-dose-${i}`} hint={t("rx.doseHint")}>
                <Input id={`rx-dose-${i}`} required value={item.dose} onChange={(e) => update(i, "dose", e.target.value)} />
              </Field>
              <Field label={t("rx.days")} htmlFor={`rx-days-${i}`}>
                <Input
                  id={`rx-days-${i}`}
                  type="number"
                  min={1}
                  max={365}
                  required
                  value={item.duration_days}
                  onChange={(e) => update(i, "duration_days", e.target.value)}
                />
              </Field>
              <Field label={t("rx.instructionsEn")} htmlFor={`rx-ien-${i}`} className="col-span-2 sm:col-span-3">
                <Input id={`rx-ien-${i}`} value={item.instructions_en ?? ""} onChange={(e) => update(i, "instructions_en", e.target.value)} />
              </Field>
              <Field label={t("rx.instructionsHi")} htmlFor={`rx-ihi-${i}`} className="col-span-2 sm:col-span-4">
                <Input id={`rx-ihi-${i}`} lang="hi" value={item.instructions_hi ?? ""} onChange={(e) => update(i, "instructions_hi", e.target.value)} />
              </Field>
            </fieldset>
          ))}
          <Button type="button" variant="outline" size="sm" onClick={() => setItems((l) => [...l, blank()])}>
            {t("rx.addItem")}
          </Button>
          <Field label={t("rx.adviceEn")} htmlFor="rx-advice-en">
            <Textarea id="rx-advice-en" rows={2} value={adviceEn} onChange={(e) => setAdviceEn(e.target.value)} />
          </Field>
          <Field label={t("rx.adviceHi")} htmlFor="rx-advice-hi">
            <Textarea id="rx-advice-hi" lang="hi" rows={2} value={adviceHi} onChange={(e) => setAdviceHi(e.target.value)} />
          </Field>
          {save.isError && <Alert tone="error">{errorMessage(save.error)}</Alert>}
          {save.isSuccess && <Alert tone="success">{t("rx.saved")}</Alert>}
          <Button type="submit" disabled={save.isPending}>
            {t("rx.save")}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
