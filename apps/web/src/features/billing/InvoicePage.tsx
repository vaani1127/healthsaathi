import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Badge, Field, Input, Select } from "@/components/ui/form";
import { api, call, type Schemas } from "@/lib/api/client";
import { errorMessage, formatDateTime, formatMoney } from "@/lib/format";

type Method = Schemas["PaymentMethod"];

export function InvoicePage() {
  const { t } = useTranslation();
  const { invoiceId } = useParams({ from: "/app/reception/invoices/$invoiceId" });
  const client = useQueryClient();
  const key = ["invoice", invoiceId];
  const invoice = useQuery({
    queryKey: key,
    queryFn: () => call(() => api.GET("/api/v1/invoices/{invoice_id}", { params: { path: { invoice_id: invoiceId } } })),
  });
  const refresh = (inv: Schemas["InvoiceOut"]) => client.setQueryData(key, inv);
  const issue = useMutation({
    mutationFn: () =>
      call(() => api.POST("/api/v1/invoices/{invoice_id}/issue", { params: { path: { invoice_id: invoiceId } } })),
    onSuccess: refresh,
  });
  const voidIt = useMutation({
    mutationFn: () =>
      call(() => api.POST("/api/v1/invoices/{invoice_id}/void", { params: { path: { invoice_id: invoiceId } } })),
    onSuccess: refresh,
  });

  if (invoice.isError) {
    return <Alert tone="error">{errorMessage(invoice.error)}</Alert>;
  }
  const inv = invoice.data;
  if (!inv) {
    return <p role="status">{t("common.loading")}</p>;
  }
  const due = inv.total_paise - inv.paid_paise;
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          {t("billing.invoice")} <Badge tone={inv.status === "paid" ? "success" : "default"}>{t(`invoiceStatus.${inv.status}`)}</Badge>
        </CardTitle>
        <Link
          to="/reception/patients/$patientId"
          params={{ patientId: inv.patient_id }}
          className="text-sm text-primary underline"
        >
          {t("billing.backToPatient")}
        </Link>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <table className="w-full text-sm">
          <tbody>
            {inv.items.map((i) => (
              <tr key={i.id} className="border-b">
                <td className="py-1">{i.service_name}</td>
                <td className="text-right">× {i.qty}</td>
                <td className="text-right">{formatMoney(i.price_paise * i.qty)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="text-right font-semibold" data-testid="invoice-total">
          {t("billing.total", { amount: formatMoney(inv.total_paise) })}
        </p>
        {inv.payments.length > 0 && (
          <ul className="text-sm text-muted-foreground">
            {inv.payments.map((p) => (
              <li key={p.id}>
                {formatDateTime(p.received_at)} · {t(`paymentMethod.${p.method}`)} · {formatMoney(p.amount_paise)}
              </li>
            ))}
          </ul>
        )}
        {(issue.isError || voidIt.isError) && <Alert tone="error">{errorMessage(issue.error ?? voidIt.error)}</Alert>}
        {inv.status === "draft" && (
          <div className="flex gap-2">
            <Button onClick={() => issue.mutate()} disabled={issue.isPending}>
              {t("billing.issue")}
            </Button>
            <Button variant="ghost" onClick={() => voidIt.mutate()} disabled={voidIt.isPending}>
              {t("billing.void")}
            </Button>
          </div>
        )}
        {inv.status === "issued" && <PaymentForm invoiceId={invoiceId} due={due} onPaid={refresh} />}
        {inv.status === "paid" && (
          <Link
            to="/print/receipt/$invoiceId"
            params={{ invoiceId }}
            className="inline-flex h-10 items-center justify-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground"
          >
            {t("billing.printReceipt")}
          </Link>
        )}
      </CardContent>
    </Card>
  );
}

function PaymentForm({
  invoiceId,
  due,
  onPaid,
}: {
  invoiceId: string;
  due: number;
  onPaid: (inv: Schemas["InvoiceOut"]) => void;
}) {
  const { t } = useTranslation();
  const [method, setMethod] = useState<Method>("cash");
  const [amount, setAmount] = useState((due / 100).toFixed(2));
  const pay = useMutation({
    mutationFn: () =>
      call(() =>
        api.POST("/api/v1/invoices/{invoice_id}/payments", {
          params: { path: { invoice_id: invoiceId } },
          body: { method, amount_paise: Math.round(Number(amount) * 100) },
        }),
      ),
    onSuccess: onPaid,
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    pay.mutate();
  };
  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
      <Field label={t("billing.method")} htmlFor="pay-method">
        <Select id="pay-method" value={method} onChange={(e) => setMethod(e.target.value as Method)}>
          {(["cash", "upi", "card_offline"] as const).map((m) => (
            <option key={m} value={m}>
              {t(`paymentMethod.${m}`)}
            </option>
          ))}
        </Select>
      </Field>
      <Field label={t("billing.amount")} htmlFor="pay-amount">
        <Input id="pay-amount" inputMode="decimal" required value={amount} onChange={(e) => setAmount(e.target.value)} />
      </Field>
      <Button type="submit" disabled={pay.isPending}>
        {t("billing.markPaid")}
      </Button>
      {pay.isError && <Alert tone="error">{errorMessage(pay.error)}</Alert>}
    </form>
  );
}
