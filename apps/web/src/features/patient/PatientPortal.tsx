import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { Receipt } from "@healthsaathi/receipt-verify";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Badge, Textarea } from "@/components/ui/form";
import { DocumentLink, ResultValues } from "@/features/common/LabResult";
import { VitalsList } from "@/features/common/VitalsList";
import { accessSentence } from "@/features/patient/explain";
import { checkReceipt, downloadReceipt } from "@/features/verify/check";
import { ReceiptResultView } from "@/features/verify/ReceiptResultView";
import { api, ApiError, call, type Schemas } from "@/lib/api/client";
import { errorMessage, formatDate, formatDateTime, formatMoney, formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";

type Tab = "record" | "log" | "consent" | "download";

export function PatientPortal() {
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>("record");
  const me = useQuery({ queryKey: ["me-patient"], queryFn: () => call(() => api.GET("/api/v1/me/patient")) });

  if (me.isError) {
    return <Alert tone="error">{errorMessage(me.error)}</Alert>;
  }
  if (!me.data) {
    return <p role="status">{t("common.loading")}</p>;
  }
  const patientId = me.data.id;
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">{t("portal.hello", { name: me.data.name })}</h1>
      <nav className="flex gap-1 overflow-x-auto border-b" role="tablist">
        {(["record", "log", "consent", "download"] as const).map((key) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            className={cn(
              "whitespace-nowrap px-3 py-2 text-sm",
              tab === key ? "border-b-2 border-primary font-semibold text-primary" : "text-muted-foreground",
            )}
            onClick={() => setTab(key)}
          >
            {t(`portal.tabs.${key}`)}
          </button>
        ))}
      </nav>
      {tab === "record" && <MyRecord patientId={patientId} />}
      {tab === "log" && <AccessLog patientId={patientId} />}
      {tab === "consent" && <ConsentTab patientId={patientId} />}
      {tab === "download" && <Download patientId={patientId} />}
    </div>
  );
}

function usePatientChart(patientId: string) {
  return useQuery({
    queryKey: ["chart", patientId],
    queryFn: () =>
      call(() => api.GET("/api/v1/patients/{patient_id}/chart", { params: { path: { patient_id: patientId } } })),
    select: (c) => (c.view === "patient" ? c : null),
  });
}

function MyRecord({ patientId }: { patientId: string }) {
  const { t } = useTranslation();
  const chart = usePatientChart(patientId);
  const data = chart.data;
  if (chart.isError) {
    return <Alert tone="error">{errorMessage(chart.error)}</Alert>;
  }
  if (!data) {
    return <p role="status">{t("common.loading")}</p>;
  }
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>{t("rx.history")}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 text-sm">
          {data.prescriptions.length === 0 && <p className="text-muted-foreground">{t("rx.none")}</p>}
          {data.prescriptions.map((rx) => (
            <article key={rx.id} className="rounded-md border p-2">
              <p className="text-xs text-muted-foreground">{formatDate(rx.signed_at ?? rx.created_at)}</p>
              <ul>
                {rx.items.map((item, i) => (
                  <li key={i}>
                    <strong>{String(item.drug)}</strong> {String(item.strength ?? "")} · {String(item.dose)} ·{" "}
                    {t("portal.forDays", { days: Number(item.duration_days) })}
                    {item.instructions_hi ? <span lang="hi"> · {String(item.instructions_hi)}</span> : null}
                  </li>
                ))}
              </ul>
              {rx.advice_hi && <p lang="hi">{rx.advice_hi}</p>}
              {rx.advice_en && <p>{rx.advice_en}</p>}
            </article>
          ))}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>{t("portal.labResults")}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 text-sm">
          {data.lab_orders.length === 0 && <p className="text-muted-foreground">{t("portal.noResults")}</p>}
          {data.lab_orders.map((o) => (
            <article key={o.id} className="rounded-md border p-2">
              <p className="font-medium">{o.tests.map((x) => String(x.name ?? x.code)).join(", ")}</p>
              <ResultValues values={o.result?.values ?? {}} />
              {o.result?.document_id && (
                <DocumentLink id={o.result.document_id} label={t("portal.downloadReport")} />
              )}
            </article>
          ))}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>{t("reception.appointments")}</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="divide-y text-sm">
            {data.appointments.map((a) => (
              <li key={a.id} className="flex justify-between py-1">
                <span>{formatDateTime(a.slot_start)}</span>
                <span className="text-muted-foreground">{t(`appointmentStatus.${a.status}`)}</span>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>{t("allergy.title")}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          <div className="flex flex-wrap gap-2">
            {data.allergies.filter((a) => a.is_active).map((a) => (
              <Badge key={a.id} tone="danger">
                {a.substance}
              </Badge>
            ))}
            {data.allergies.every((a) => !a.is_active) && (
              <span className="text-sm text-muted-foreground">{t("allergy.none")}</span>
            )}
          </div>
          <VitalsList vitals={data.vitals.slice(0, 3)} />
        </CardContent>
      </Card>
      <Card className="md:col-span-2">
        <CardHeader>
          <CardTitle>{t("portal.bills")}</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="divide-y text-sm">
            {data.invoices.map((inv) => (
              <li key={inv.id} className="flex justify-between py-1">
                <span>{formatDate(inv.created_at)}</span>
                <span>
                  {formatMoney(inv.total_paise)} · {t(`invoiceStatus.${inv.status}`)}
                </span>
              </li>
            ))}
            {data.invoices.length === 0 && <li className="text-muted-foreground">{t("portal.noBills")}</li>}
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}

function AccessLog({ patientId }: { patientId: string }) {
  const { t } = useTranslation();
  const log = useInfiniteQuery({
    queryKey: ["access-log", patientId],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      call(() =>
        api.GET("/api/v1/patients/{patient_id}/access-log", {
          params: { path: { patient_id: patientId }, query: pageParam ? { cursor: pageParam } : {} },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  if (log.isError) {
    return <Alert tone="error">{errorMessage(log.error)}</Alert>;
  }
  const entries = log.data?.pages.flatMap((p) => p.items) ?? [];
  const byDay = new Map<string, Schemas["AccessLogEntry"][]>();
  for (const e of entries) {
    const day = formatDate(e.at);
    byDay.set(day, [...(byDay.get(day) ?? []), e]);
  }
  return (
    <section className="flex flex-col gap-4" aria-label={t("portal.tabs.log")}>
      <p className="text-sm text-muted-foreground">{t("portal.logIntro")}</p>
      {log.isPending && <p role="status">{t("common.loading")}</p>}
      {!log.isPending && entries.length === 0 && <p>{t("portal.logEmpty")}</p>}
      {[...byDay.entries()].map(([day, items]) => (
        <div key={day}>
          <h2 className="mb-2 font-semibold">{day}</h2>
          <ul className="flex flex-col gap-2">
            {items.map((e) => (
              <AccessRow key={e.id} entry={e} patientId={patientId} />
            ))}
          </ul>
        </div>
      ))}
      {log.hasNextPage && (
        <Button variant="outline" onClick={() => void log.fetchNextPage()} disabled={log.isFetchingNextPage}>
          {t("portal.more")}
        </Button>
      )}
    </section>
  );
}

function AccessRow({ entry, patientId }: { entry: Schemas["AccessLogEntry"]; patientId: string }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [message, setMessage] = useState("");
  const report = useMutation({
    mutationFn: () =>
      call(() =>
        api.POST("/api/v1/access-events/{access_event_id}/query", {
          params: { path: { access_event_id: entry.id } },
          body: { message },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["access-log", patientId] }),
  });
  const verify = useMutation({
    mutationFn: async () => {
      const receipt = (await call(() =>
        api.GET("/api/v1/access-events/{access_event_id}/receipt", {
          params: { path: { access_event_id: entry.id } },
        }),
      )) as unknown as Receipt;
      return { receipt, result: await checkReceipt(receipt) };
    },
  });
  const notReady = verify.error instanceof ApiError && verify.error.status === 409;
  return (
    <li className="rounded-md border p-3 text-sm" data-testid="access-entry">
      <div className="flex items-start justify-between gap-2">
        <p>{accessSentence(entry)}</p>
        <span className="shrink-0 text-xs text-muted-foreground">{formatTime(entry.at)}</span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {entry.break_glass && <Badge tone="danger">{t("templates.T_BREAKGLASS")}</Badge>}
        {entry.query_status ? (
          <Badge tone="warning">{t(`portal.queryStatus.${entry.query_status}`)}</Badge>
        ) : (
          !open && (
            <Button variant="ghost" size="sm" className="px-0 text-primary underline" onClick={() => setOpen(true)}>
              {t("portal.notMe")}
            </Button>
          )
        )}
        {!verify.data && (
          <Button
            variant="ghost"
            size="sm"
            className="px-0 text-primary underline"
            disabled={verify.isPending}
            onClick={() => verify.mutate()}
          >
            {t("verify.checkThis")}
          </Button>
        )}
      </div>
      {notReady && <p className="mt-2 text-xs text-muted-foreground">{t("verify.notReady")}</p>}
      {verify.isError && !notReady && <Alert tone="error">{errorMessage(verify.error)}</Alert>}
      {verify.data && (
        <div className="mt-2 flex flex-col gap-2 rounded-md bg-muted p-2">
          <ReceiptResultView result={verify.data.result} />
          <Button size="sm" variant="outline" onClick={() => downloadReceipt(verify.data.receipt)}>
            {t("verify.download")}
          </Button>
        </div>
      )}
      {open && !entry.query_status && (
        <div className="mt-2 flex flex-col gap-2">
          <Textarea
            aria-label={t("portal.notMeMessage")}
            placeholder={t("portal.notMeMessage")}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
          />
          {report.isError && <Alert tone="error">{errorMessage(report.error)}</Alert>}
          <Button size="sm" disabled={message.trim().length < 3 || report.isPending} onClick={() => report.mutate()}>
            {t("portal.sendReport")}
          </Button>
        </div>
      )}
    </li>
  );
}

function ConsentTab({ patientId }: { patientId: string }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const consents = useQuery({
    queryKey: ["consents", patientId],
    queryFn: () =>
      call(() => api.GET("/api/v1/patients/{patient_id}/consents", { params: { path: { patient_id: patientId } } })),
  });
  const withdraw = useMutation({
    mutationFn: (consentId: string) =>
      call(() => api.POST("/api/v1/consents/{consent_id}/withdraw", { params: { path: { consent_id: consentId } } })),
    onSuccess: () => client.invalidateQueries({ queryKey: ["consents", patientId] }),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("portal.consentTitle")}</CardTitle>
        <p className="text-sm text-muted-foreground">{t("portal.consentExplain")}</p>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        {consents.data?.length === 0 && <p>{t("consent.missing")}</p>}
        {consents.data?.map((c) => (
          <article key={c.id} className="rounded-md border p-2">
            <p className="font-medium">{t("consent.given", { version: c.notice_version })}</p>
            <ul className="text-muted-foreground">
              {c.events.map((e) => (
                <li key={e.id}>
                  {t(`portal.consentEvent.${e.kind}`)} · {formatDateTime(e.at)}
                </li>
              ))}
            </ul>
            {!c.withdrawn_at && (
              <Button
                variant="destructive"
                size="sm"
                className="mt-2"
                disabled={withdraw.isPending}
                onClick={() => {
                  if (window.confirm(t("portal.withdrawConfirm"))) {
                    withdraw.mutate(c.id);
                  }
                }}
              >
                {t("portal.withdraw")}
              </Button>
            )}
          </article>
        ))}
        {withdraw.isError && <Alert tone="error">{errorMessage(withdraw.error)}</Alert>}
      </CardContent>
    </Card>
  );
}

function Download({ patientId }: { patientId: string }) {
  const { t } = useTranslation();
  const download = useMutation({
    mutationFn: async () => {
      const data = await call(() =>
        api.GET("/api/v1/patients/{patient_id}/export", { params: { path: { patient_id: patientId } } }),
      );
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `healthsaathi-record-${data.record.patient.mrn}.json`;
      a.click();
      URL.revokeObjectURL(url);
    },
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("portal.downloadTitle")}</CardTitle>
        <p className="text-sm text-muted-foreground">{t("portal.downloadExplain")}</p>
      </CardHeader>
      <CardContent className="flex flex-wrap gap-2">
        <Button onClick={() => download.mutate()} disabled={download.isPending}>
          {t("portal.downloadJson")}
        </Button>
        <Link to="/patient/print" className="inline-flex h-10 items-center rounded-md border px-4 text-sm">
          {t("portal.printRecord")}
        </Link>
        {download.isError && <Alert tone="error">{errorMessage(download.error)}</Alert>}
      </CardContent>
    </Card>
  );
}
