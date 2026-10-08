import "@fontsource/noto-sans-devanagari/400.css";
import "@fontsource/noto-sans-devanagari/700.css";

import { useQuery } from "@tanstack/react-query";
import { useParams, useSearch } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/form";
import { useStaffNames } from "@/features/common/queries";
import { api, call } from "@/lib/api/client";
import en from "@/locales/en.json";
import hi from "@/locales/hi.json";
import { errorMessage, formatDate } from "@/lib/format";

/** Bilingual prescription for the browser's print dialog (save as PDF works too). */
export function PrescriptionPrint() {
  const { t } = useTranslation();
  const { rxId } = useParams({ from: "/app/print/prescription/$rxId" });
  const { patient: patientId } = useSearch({ from: "/app/print/prescription/$rxId" });
  const names = useStaffNames();

  const data = useQuery({
    queryKey: ["print-rx", rxId],
    retry: false,
    queryFn: async () => {
      const [rx, patient, clinic] = await Promise.all([
        call(() => api.GET("/api/v1/prescriptions/{rx_id}/print", { params: { path: { rx_id: rxId } } })),
        call(() => api.GET("/api/v1/patients/{patient_id}", { params: { path: { patient_id: patientId } } })),
        call(() => api.GET("/api/v1/clinic")),
      ]);
      return { rx, patient, clinic };
    },
  });

  if (data.isError) {
    return <Alert tone="error">{errorMessage(data.error)}</Alert>;
  }
  if (!data.data) {
    return <p role="status">{t("common.loading")}</p>;
  }
  const { rx, patient, clinic } = data.data;
  const label = (key: keyof typeof en.print) => `${en.print[key]} / ${hi.print[key]}`;

  return (
    <article className="mx-auto max-w-2xl bg-white p-6 text-black print:p-0" style={{ fontFamily: '"Noto Sans", "Noto Sans Devanagari", sans-serif' }}>
      <div className="mb-4 flex justify-end print:hidden">
        <Button onClick={() => window.print()}>{t("rx.printNow")}</Button>
      </div>
      <header className="border-b-2 border-black pb-2">
        <h1 className="text-2xl font-bold">{clinic.name}</h1>
        <p>
          {clinic.city}, {clinic.state}
        </p>
        <p className="mt-1">
          {names.data?.get(rx.author_user_id)} · {formatDate(rx.signed_at ?? rx.created_at)}
        </p>
      </header>
      <section className="my-3 grid grid-cols-2 gap-1 text-sm">
        <p>
          <strong>{label("patient")}:</strong> {patient.name}
        </p>
        <p>
          <strong>{label("mrn")}:</strong> {patient.mrn}
        </p>
      </section>
      <h2 className="mt-4 text-lg font-bold">Rx</h2>
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-black text-left">
            <th className="py-1">{label("medicine")}</th>
            <th>{label("dose")}</th>
            <th>{label("days")}</th>
          </tr>
        </thead>
        <tbody>
          {rx.items.map((item, i) => (
            <tr key={i} className="border-b align-top">
              <td className="py-1">
                <strong>{String(item.drug)}</strong> {String(item.strength ?? "")}
                {item.instructions_en ? <div>{String(item.instructions_en)}</div> : null}
                {item.instructions_hi ? <div lang="hi">{String(item.instructions_hi)}</div> : null}
              </td>
              <td>{String(item.dose)}</td>
              <td>{String(item.duration_days)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {(rx.advice_en || rx.advice_hi) && (
        <section className="mt-4">
          <h3 className="font-bold">{label("advice")}</h3>
          {rx.advice_en && <p>{rx.advice_en}</p>}
          {rx.advice_hi && <p lang="hi">{rx.advice_hi}</p>}
        </section>
      )}
      <footer className="mt-10 text-right text-sm">
        <p>{label("signedBy")}</p>
        <p className="font-semibold">{names.data?.get(rx.author_user_id)}</p>
      </footer>
    </article>
  );
}
