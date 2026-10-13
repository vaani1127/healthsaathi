import "@fontsource/noto-sans-devanagari/400.css";
import "@fontsource/noto-sans-devanagari/700.css";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Loading, LoadError } from "@/components/ui/state";
import { api, call } from "@/lib/api/client";
import { formatDate, formatDateTime } from "@/lib/format";
import en from "@/locales/en.json";
import hi from "@/locales/hi.json";

type TestCode = keyof typeof en.lab.tests;

function testName(test: Record<string, unknown>): string {
  const code = String(test.code ?? "");
  if (code in en.lab.tests) {
    const key = code as TestCode;
    return `${en.lab.tests[key]} / ${hi.lab.tests[key]}`;
  }
  return String(test.name ?? code);
}

/** Bilingual lab report for the browser's print dialog (SPEC 9). Only released results print. */
export function LabReportPrint() {
  const { t } = useTranslation();
  const { orderId } = useParams({ from: "/app/print/lab/$orderId" });
  const data = useQuery({
    queryKey: ["print-lab", orderId],
    retry: false,
    queryFn: async () => {
      const [report, clinic] = await Promise.all([
        call(() => api.GET("/api/v1/lab-orders/{order_id}/print", { params: { path: { order_id: orderId } } })),
        call(() => api.GET("/api/v1/clinic")),
      ]);
      return { report, clinic };
    },
  });

  if (data.isError) {
    return <LoadError error={data.error} />;
  }
  if (!data.data) {
    return <Loading />;
  }
  const { report, clinic } = data.data;
  const { order, patient } = report;
  const result = order.result;
  const label = (key: keyof typeof en.print) => `${en.print[key]} / ${hi.print[key]}`;
  const values = Object.entries(result?.values ?? {});

  return (
    <article
      className="print-font mx-auto max-w-2xl bg-white p-6 text-black print:p-0"
     
    >
      <div className="mb-4 flex justify-end print:hidden">
        <Button onClick={() => window.print()}>{t("rx.printNow")}</Button>
      </div>
      <header className="border-b-2 border-black pb-2">
        <h1 className="text-2xl font-bold">{clinic.name}</h1>
        <p>
          {clinic.city}, {clinic.state}
        </p>
        <p className="mt-1 text-lg font-semibold">{label("labReport")}</p>
      </header>
      <section className="my-3 grid grid-cols-2 gap-1 text-sm">
        <p>
          <strong>{label("patient")}:</strong> {patient.name}
        </p>
        <p>
          <strong>{label("mrn")}:</strong> {patient.mrn}
        </p>
        <p>
          <strong>{label("orderedBy")}:</strong> {report.ordered_by_name}
        </p>
        <p>
          <strong>{label("orderedOn")}:</strong> {formatDate(order.created_at)}
        </p>
        <p className="col-span-2">
          <strong>{label("tests")}:</strong> {order.tests.map(testName).join(", ")}
        </p>
      </section>
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-black text-left">
            <th className="py-1">{label("test")}</th>
            <th>{label("value")}</th>
            <th>{label("unit")}</th>
          </tr>
        </thead>
        <tbody>
          {values.length === 0 && (
            <tr>
              <td colSpan={3} className="py-2">
                {label("seeAttached")}
              </td>
            </tr>
          )}
          {values.map(([key, raw]) => {
            const v = raw as { value?: unknown; unit?: unknown };
            return (
              <tr key={key} className="border-b align-top">
                <td className="py-1 font-semibold">{key}</td>
                <td>{String(v.value ?? raw)}</td>
                <td>{v.unit ? String(v.unit) : ""}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <footer className="mt-10 flex justify-between text-sm">
        <p>
          {label("resultedOn")}: {result ? formatDateTime(result.resulted_at) : ""}
        </p>
        <div className="text-right">
          <p>{label("resultedBy")}</p>
          <p className="font-semibold">{report.resulted_by_name}</p>
        </div>
      </footer>
    </article>
  );
}
