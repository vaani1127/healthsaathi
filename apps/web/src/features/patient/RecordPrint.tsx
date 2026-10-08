import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/form";
import { ResultValues } from "@/features/common/LabResult";
import { api, call } from "@/lib/api/client";
import { errorMessage, formatDate } from "@/lib/format";

/** The patient's own record as a page to print or save as PDF. */
export function RecordPrint() {
  const { t } = useTranslation();
  const data = useQuery({
    queryKey: ["record-print"],
    queryFn: async () => {
      const me = await call(() => api.GET("/api/v1/me/patient"));
      const chart = await call(() =>
        api.GET("/api/v1/patients/{patient_id}/chart", { params: { path: { patient_id: me.id } } }),
      );
      return chart.view === "patient" ? chart : null;
    },
  });
  if (data.isError) {
    return <Alert tone="error">{errorMessage(data.error)}</Alert>;
  }
  const c = data.data;
  if (!c) {
    return <p role="status">{t("common.loading")}</p>;
  }
  return (
    <article className="mx-auto flex max-w-2xl flex-col gap-4 bg-white p-4 text-black">
      <div className="flex justify-end print:hidden">
        <Button onClick={() => window.print()}>{t("rx.printNow")}</Button>
      </div>
      <header>
        <h1 className="text-2xl font-bold">{c.patient.name}</h1>
        <p>
          {c.patient.mrn} · {t(`sex.${c.patient.sex}`)}
          {c.patient.dob && ` · ${formatDate(c.patient.dob)}`}
        </p>
      </header>
      <section>
        <h2 className="font-bold">{t("allergy.title")}</h2>
        <p>{c.allergies.filter((a) => a.is_active).map((a) => a.substance).join(", ") || t("allergy.none")}</p>
      </section>
      <section>
        <h2 className="font-bold">{t("conditions.title")}</h2>
        <ul>
          {c.conditions.map((x) => (
            <li key={x.id}>{x.text}</li>
          ))}
        </ul>
      </section>
      <section>
        <h2 className="font-bold">{t("rx.history")}</h2>
        {c.prescriptions.map((rx) => (
          <div key={rx.id} className="border-b py-1">
            <p className="text-sm">{formatDate(rx.signed_at ?? rx.created_at)}</p>
            <ul>
              {rx.items.map((item, i) => (
                <li key={i}>
                  {String(item.drug)} {String(item.strength ?? "")} · {String(item.dose)} ·{" "}
                  {t("portal.forDays", { days: Number(item.duration_days) })}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </section>
      <section>
        <h2 className="font-bold">{t("portal.labResults")}</h2>
        {c.lab_orders.map((o) => (
          <div key={o.id} className="border-b py-1">
            <p>{o.tests.map((x) => String(x.name ?? x.code)).join(", ")}</p>
            <ResultValues values={o.result?.values ?? {}} />
          </div>
        ))}
      </section>
      <section>
        <h2 className="font-bold">{t("notes.history")}</h2>
        {c.notes.map((n) => (
          <div key={n.id} className="border-b py-1">
            <p className="text-sm">{formatDate(n.created_at)}</p>
            <p className="whitespace-pre-wrap">{n.body}</p>
          </div>
        ))}
      </section>
    </article>
  );
}
