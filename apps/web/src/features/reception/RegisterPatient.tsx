import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Alert, Field, Input, Select } from "@/components/ui/form";
import { api, call, type Schemas } from "@/lib/api/client";
import { errorMessage } from "@/lib/format";

type Sex = Schemas["Sex"];

export function RegisterPatient({ onCancel }: { onCancel: () => void }) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const [form, setForm] = useState({ name: "", sex: "" as Sex | "", dob: "", phone: "", address: "" });
  const [agreed, setAgreed] = useState(false);

  const notice = useQuery({
    queryKey: ["consent-notice"],
    queryFn: () => call(() => api.GET("/api/v1/consent-notices/current")),
  });

  const register = useMutation({
    mutationFn: async () => {
      const patient = await call(() =>
        api.POST("/api/v1/patients", {
          body: {
            name: form.name.trim(),
            sex: form.sex as Sex,
            dob: form.dob || null,
            phone: form.phone || null,
            address: form.address || null,
          },
        }),
      );
      await call(() =>
        api.POST("/api/v1/patients/{patient_id}/consents", { params: { path: { patient_id: patient.id } } }),
      );
      return patient;
    },
    onSuccess: (patient) =>
      navigate({ to: "/reception/patients/$patientId", params: { patientId: patient.id } }),
  });

  const set = (key: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));

  const submit = (e: FormEvent) => {
    e.preventDefault();
    register.mutate();
  };

  const text = notice.data ? (i18n.language === "hi" ? notice.data.text_hi : notice.data.text_en) : null;

  return (
    <form onSubmit={submit} className="flex flex-col gap-3 rounded-md border p-3">
      <h3 className="font-semibold">{t("reception.registerNew")}</h3>
      <Field label={t("patient.name")} htmlFor="reg-name">
        <Input id="reg-name" required minLength={2} value={form.name} onChange={set("name")} />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label={t("patient.sex")} htmlFor="reg-sex">
          <Select id="reg-sex" required value={form.sex} onChange={set("sex")}>
            <option value="">{t("common.choose")}</option>
            {(["female", "male", "other", "unknown"] as const).map((s) => (
              <option key={s} value={s}>
                {t(`sex.${s}`)}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={t("patient.dob")} htmlFor="reg-dob">
          <Input id="reg-dob" type="date" value={form.dob} onChange={set("dob")} />
        </Field>
      </div>
      <Field label={t("patient.phone")} htmlFor="reg-phone">
        <Input id="reg-phone" type="tel" value={form.phone} onChange={set("phone")} />
      </Field>
      <Field label={t("patient.address")} htmlFor="reg-address">
        <Input id="reg-address" value={form.address} onChange={set("address")} />
      </Field>

      <section aria-labelledby="consent-title" className="rounded-md bg-muted p-3 text-sm">
        <h4 id="consent-title" className="mb-1 font-semibold">
          {t("consent.noticeTitle", { version: notice.data?.version ?? "" })}
        </h4>
        {text ? <p data-testid="consent-text">{text}</p> : <p>{t("consent.noNotice")}</p>}
        <label className="mt-2 flex items-start gap-2">
          <input
            type="checkbox"
            className="mt-1 size-4"
            checked={agreed}
            onChange={(e) => setAgreed(e.target.checked)}
          />
          <span>{t("consent.agreed")}</span>
        </label>
      </section>

      {register.isError && <Alert tone="error">{errorMessage(register.error)}</Alert>}
      <div className="flex gap-2">
        <Button type="submit" disabled={!agreed || !text || register.isPending}>
          {t("reception.register")}
        </Button>
        <Button type="button" variant="ghost" onClick={onCancel}>
          {t("common.cancel")}
        </Button>
      </div>
    </form>
  );
}
