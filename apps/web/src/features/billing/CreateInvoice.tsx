import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Badge, Input } from "@/components/ui/form";
import { api, call, type Schemas } from "@/lib/api/client";
import { errorMessage, formatDate, formatMoney } from "@/lib/format";

export function CreateInvoice({
  patientId,
  invoices,
}: {
  patientId: string;
  invoices: Schemas["InvoiceSummary"][];
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const services = useQuery({
    queryKey: ["services"],
    queryFn: () => call(() => api.GET("/api/v1/services")),
  });
  const [qty, setQty] = useState<Record<string, number>>({});
  const chosen = Object.entries(qty).filter(([, n]) => n > 0);
  const total = chosen.reduce(
    (sum, [id, n]) => sum + n * (services.data?.find((s) => s.id === id)?.price_paise ?? 0),
    0,
  );

  const create = useMutation({
    mutationFn: () =>
      call(() =>
        api.POST("/api/v1/patients/{patient_id}/invoices", {
          params: { path: { patient_id: patientId } },
          body: { items: chosen.map(([service_id, n]) => ({ service_id, qty: n })) },
        }),
      ),
    onSuccess: (inv) => navigate({ to: "/reception/invoices/$invoiceId", params: { invoiceId: inv.id } }),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("billing.title")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {invoices.length > 0 && (
          <ul className="divide-y text-sm">
            {invoices.map((inv) => (
              <li key={inv.id} className="flex items-center justify-between py-1">
                <Link to="/reception/invoices/$invoiceId" params={{ invoiceId: inv.id }} className="underline">
                  {formatDate(inv.created_at)} · {formatMoney(inv.total_paise)}
                </Link>
                <Badge tone={inv.status === "paid" ? "success" : "default"}>{t(`invoiceStatus.${inv.status}`)}</Badge>
              </li>
            ))}
          </ul>
        )}
        <ul className="flex flex-col gap-2">
          {services.data?.map((s) => (
            <li key={s.id} className="flex items-center justify-between gap-2">
              <label htmlFor={`svc-${s.id}`} className="flex-1 text-sm">
                {s.name} <span className="text-muted-foreground">{formatMoney(s.price_paise)}</span>
              </label>
              <Input
                id={`svc-${s.id}`}
                type="number"
                min={0}
                max={100}
                className="w-20"
                value={qty[s.id] ?? 0}
                onChange={(e) => setQty((q) => ({ ...q, [s.id]: Number(e.target.value) }))}
              />
            </li>
          ))}
        </ul>
        <p className="text-right font-semibold">{t("billing.total", { amount: formatMoney(total) })}</p>
        {create.isError && <Alert tone="error">{errorMessage(create.error)}</Alert>}
        <Button disabled={chosen.length === 0 || create.isPending} onClick={() => create.mutate()}>
          {t("billing.create")}
        </Button>
      </CardContent>
    </Card>
  );
}
