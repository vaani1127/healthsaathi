import "@fontsource/noto-sans-devanagari/400.css";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/form";
import { api, call } from "@/lib/api/client";
import { errorMessage, formatDateTime, formatMoney } from "@/lib/format";
import en from "@/locales/en.json";
import hi from "@/locales/hi.json";

export function ReceiptPrint() {
  const { t } = useTranslation();
  const { invoiceId } = useParams({ from: "/app/print/receipt/$invoiceId" });
  const data = useQuery({
    queryKey: ["receipt", invoiceId],
    retry: false,
    queryFn: async () => {
      const receipt = await call(() =>
        api.GET("/api/v1/invoices/{invoice_id}/receipt", { params: { path: { invoice_id: invoiceId } } }),
      );
      const [patient, clinic] = await Promise.all([
        call(() => api.GET("/api/v1/patients/{patient_id}", { params: { path: { patient_id: receipt.patient_id } } })),
        call(() => api.GET("/api/v1/clinic")),
      ]);
      return { receipt, patient, clinic };
    },
  });
  if (data.isError) {
    return <Alert tone="error">{errorMessage(data.error)}</Alert>;
  }
  if (!data.data) {
    return <p role="status">{t("common.loading")}</p>;
  }
  const { receipt, patient, clinic } = data.data;
  const label = (key: keyof typeof en.receipt) => `${en.receipt[key]} / ${hi.receipt[key]}`;
  return (
    <article className="mx-auto max-w-md bg-white p-6 text-black" style={{ fontFamily: '"Noto Sans", "Noto Sans Devanagari", sans-serif' }}>
      <div className="mb-4 flex justify-end print:hidden">
        <Button onClick={() => window.print()}>{t("rx.printNow")}</Button>
      </div>
      <header className="border-b-2 border-black pb-2 text-center">
        <h1 className="text-xl font-bold">{clinic.name}</h1>
        <p className="text-sm">
          {clinic.city}, {clinic.state}
        </p>
        <h2 className="mt-2 font-semibold">{label("title")}</h2>
      </header>
      <p className="my-2 text-sm">
        {label("patient")}: <strong>{patient.name}</strong> ({patient.mrn})
      </p>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-black text-left">
            <th>{label("item")}</th>
            <th className="text-right">{label("amount")}</th>
          </tr>
        </thead>
        <tbody>
          {receipt.items.map((i) => (
            <tr key={i.id} className="border-b">
              <td className="py-1">
                {i.service_name} × {i.qty}
              </td>
              <td className="text-right">{formatMoney(i.price_paise * i.qty)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-right font-bold">
        {label("total")}: {formatMoney(receipt.total_paise)}
      </p>
      <ul className="mt-2 text-sm">
        {receipt.payments.map((p) => (
          <li key={p.id}>
            {label("paid")}: {formatMoney(p.amount_paise)} · {en.paymentMethod[p.method]} / {hi.paymentMethod[p.method]} ·{" "}
            {formatDateTime(p.received_at)}
          </li>
        ))}
      </ul>
    </article>
  );
}
