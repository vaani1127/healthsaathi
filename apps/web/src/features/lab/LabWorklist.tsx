import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Badge, Field, Input } from "@/components/ui/form";
import { api, call, type Schemas } from "@/lib/api/client";
import { API_URL } from "@/lib/config";
import { errorMessage, formatDateTime } from "@/lib/format";
import { useAuth } from "@/stores/auth";

type Item = Schemas["WorklistItem"];

export function LabWorklist() {
  const { t } = useTranslation();
  const [released, setReleased] = useState(0);
  const worklist = useQuery({
    queryKey: ["worklist"],
    queryFn: () => call(() => api.GET("/api/v1/lab/worklist")),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("lab.worklist")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {worklist.isError && <Alert tone="error">{errorMessage(worklist.error)}</Alert>}
        {worklist.data?.length === 0 && <p className="text-sm text-muted-foreground">{t("lab.empty")}</p>}
        {released > 0 && <Alert tone="success">{t("lab.released")}</Alert>}
        {worklist.data?.map((item) => (
          <WorkItem key={item.order.id} item={item} onReleased={() => setReleased((n) => n + 1)} />
        ))}
      </CardContent>
    </Card>
  );
}

function WorkItem({ item, onReleased }: { item: Item; onReleased: () => void }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const order = item.order;
  const collect = useMutation({
    mutationFn: () =>
      call(() =>
        api.PATCH("/api/v1/lab-orders/{order_id}", {
          params: { path: { order_id: order.id } },
          body: { status: "collected" },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["worklist"] }),
  });
  return (
    <article className="rounded-md border p-3" data-testid="work-item">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="font-medium">
            {item.patient_name} <span className="text-sm text-muted-foreground">{item.patient_mrn}</span>
          </p>
          <p className="text-sm">{order.tests.map((x) => String(x.name ?? x.code)).join(", ")}</p>
          <p className="text-xs text-muted-foreground">{formatDateTime(order.created_at)}</p>
        </div>
        <Badge tone={order.status === "ordered" ? "warning" : "default"}>{t(`labStatus.${order.status}`)}</Badge>
      </div>
      {order.status === "ordered" && (
        <Button size="sm" className="mt-2" onClick={() => collect.mutate()} disabled={collect.isPending}>
          {t("lab.collected")}
        </Button>
      )}
      {order.status === "collected" && <ResultForm order={order} onReleased={onReleased} />}
      {collect.isError && <Alert tone="error">{errorMessage(collect.error)}</Alert>}
    </article>
  );
}

function ResultForm({ order, onReleased }: { order: Schemas["LabOrderOut"]; onReleased: () => void }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const [values, setValues] = useState<Record<string, { value: string; unit: string }>>({});
  const [file, setFile] = useState<File | null>(null);

  const save = useMutation({
    mutationFn: async () => {
      // Multipart upload; openapi-fetch does not build form bodies, so fetch is used directly.
      const form = new FormData();
      form.append("values", JSON.stringify(values));
      if (file) {
        form.append("file", file);
      }
      const send = () =>
        fetch(`${API_URL}/api/v1/lab-orders/${order.id}/result`, {
          method: "POST",
          body: form,
          credentials: "include",
          headers: { authorization: `Bearer ${useAuth.getState().token?.access_token ?? ""}` },
        });
      let resp = await send();
      if (resp.status === 401 && (await useAuth.getState().refresh())) {
        resp = await send();
      }
      if (!resp.ok) {
        const problem = (await resp.json().catch(() => ({}))) as { code?: string };
        throw new Error(problem.code ?? `http-${resp.status}`);
      }
      return (await resp.json()) as Schemas["LabOrderOut"];
    },
    onSuccess: async (resulted) => {
      await call(() =>
        api.POST("/api/v1/lab-orders/{order_id}/release", { params: { path: { order_id: resulted.id } } }),
      );
      onReleased();
      await client.invalidateQueries({ queryKey: ["worklist"] });
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate();
  };

  return (
    <form onSubmit={submit} className="mt-3 flex flex-col gap-2">
      {order.tests.map((test) => {
        const code = String(test.code);
        const current = values[code] ?? { value: "", unit: "" };
        return (
          <div key={code} className="grid grid-cols-2 gap-2">
            <Field label={t("lab.valueFor", { test: String(test.name ?? code) })} htmlFor={`val-${order.id}-${code}`}>
              <Input
                id={`val-${order.id}-${code}`}
                required
                value={current.value}
                onChange={(e) => setValues((v) => ({ ...v, [code]: { ...current, value: e.target.value } }))}
              />
            </Field>
            <Field label={t("lab.unit")} htmlFor={`unit-${order.id}-${code}`}>
              <Input
                id={`unit-${order.id}-${code}`}
                value={current.unit}
                onChange={(e) => setValues((v) => ({ ...v, [code]: { ...current, unit: e.target.value } }))}
              />
            </Field>
          </div>
        );
      })}
      <Field label={t("lab.report")} htmlFor={`file-${order.id}`} hint={t("lab.reportHint")}>
        <Input
          id={`file-${order.id}`}
          type="file"
          accept="application/pdf,image/png,image/jpeg"
          className="py-1.5"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
      </Field>
      {save.isError && <Alert tone="error">{t(`errors.${save.error.message}`, { defaultValue: t("errors.generic") })}</Alert>}
      <Button type="submit" disabled={save.isPending}>
        {t("lab.saveAndRelease")}
      </Button>
    </form>
  );
}
