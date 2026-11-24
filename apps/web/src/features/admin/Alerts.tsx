import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Confirmation } from "@/components/ui/confirmation";
import { Alert as Notice, Badge, Field, Input, Textarea } from "@/components/ui/form";
import { Empty, Loading, LoadError, QueryStatus } from "@/components/ui/state";
import { api, call, type Schemas } from "@/lib/api/client";
import { clinicDay, useClinicToday } from "@/lib/clinicDay";
import { errorMessage, formatDate, formatDateTime, formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";

type AlertItem = Schemas["AlertOut"];
type ListState = AlertItem["list_state"];
type Outcome = "benign" | "misuse" | "unsure";
const OUTCOMES: Outcome[] = ["misuse", "benign", "unsure"];

function useFeatureLabel() {
  const { t } = useTranslation();
  return (name: string) =>
    name.startsWith("tpl_") ? t(`templates.${name.slice(4)}`, { defaultValue: name }) : t(`features.${name}`, { defaultValue: name });
}

function statusTone(status: string) {
  return status === "misuse" ? "danger" : status === "benign" ? "success" : status === "unsure" ? "warning" : "default";
}

function Why({ code, strength }: { code: string | null | undefined; strength: number }) {
  const { t } = useTranslation();
  return (
    <span>
      {code ? t(`templates.${code}`) : t("admin.alerts.unexplained")}{" "}
      <span className="text-muted-foreground">({t("admin.alerts.strength", { value: strength.toFixed(2) })})</span>
    </span>
  );
}

const STATE_TONE: Record<ListState, "default" | "success" | "warning" | "danger"> = {
  provisional: "warning",
  final: "default",
  retired: "default",
  outside_final: "default",
};

/** Provisional, final, retired, or reviewed but outside the final list (ADR 0008). */
function StateBadge({ state }: { state: ListState }) {
  const { t } = useTranslation();
  return (
    <Badge tone={STATE_TONE[state]} data-testid="alert-state">
      {t(`admin.alerts.listState.${state}`)}
    </Badge>
  );
}

/** Whether the day's list is final yet, and when it will be. */
function DayNotice({ day }: { day: string }) {
  const { t } = useTranslation();
  const status = useQuery({
    queryKey: ["alert-day", day],
    queryFn: () => call(() => api.GET("/api/v1/alerts/days/{day}", { params: { path: { day } } })),
  });
  const s = status.data;
  if (!s) {
    return null;
  }
  if (s.state === "final") {
    return (
      <Notice tone="info" data-testid="day-notice">
        {t("admin.alerts.dayFinal", { day: formatDate(day), retired: s.retired_count ?? 0 })}
      </Notice>
    );
  }
  if (s.state === "skipped") {
    return (
      <Notice tone="info" data-testid="day-notice">
        {t("admin.alerts.daySkipped", { day: formatDate(day) })}
      </Notice>
    );
  }
  // The final list can be made from 00:00 clinic time two days after the day.
  const readyDay = clinicDay(s.timezone, new Date(s.ready_after));
  return (
    <Notice tone="info" data-testid="day-notice">
      {t("admin.alerts.dayProvisional", { day: formatDate(day), ready: formatDate(readyDay) })}
    </Notice>
  );
}

type View = "open" | "day";

export function Alerts() {
  const { t } = useTranslation();
  const [view, setView] = useState<View>("open");
  // Alerts are dated in the clinic's timezone, so the day view starts on the clinic's today.
  const today = useClinicToday();
  const [picked, setDay] = useState<string | null>(null);
  const day = picked ?? today;
  const [selected, setSelected] = useState<string | null>(null);
  // The result of the last review. A reviewed alert leaves the open list when it refetches,
  // so the confirmation is kept here, outside the list, until dismissed or another alert opens.
  const [confirmation, setConfirmation] = useState<string | null>(null);
  const [showRetired, setShowRetired] = useState(false);
  // Retired alerts are not open for review, so only the day view can show them.
  const includeRetired = view === "day" && showRetired;
  const alerts = useQuery({
    queryKey: ["alerts", view, view === "day" ? day : null, includeRetired],
    queryFn: () =>
      call(() =>
        api.GET("/api/v1/alerts", {
          params: {
            query: { ...(view === "open" ? { status: "open" as const } : { day }), include_retired: includeRetired },
          },
        }),
      ),
  });
  const label = useFeatureLabel();
  let lastDay: string | null = null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("admin.alerts.title")}</CardTitle>
        <p className="text-sm text-muted-foreground">{t("admin.alerts.explain")}</p>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Confirmation message={confirmation} onDismiss={() => setConfirmation(null)} testId="review-confirmation" />
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex gap-1" role="group" aria-label={t("admin.alerts.show")}>
            {(["open", "day"] as const).map((v) => (
              <Button key={v} size="sm" variant={view === v ? "default" : "outline"} aria-pressed={view === v} onClick={() => setView(v)}>
                {t(`admin.alerts.view.${v}`)}
              </Button>
            ))}
          </div>
          {view === "day" && (
            <Field label={t("admin.day")} htmlFor="alerts-day">
              <Input id="alerts-day" type="date" value={day} onChange={(e) => setDay(e.target.value)} />
            </Field>
          )}
          {view === "day" && (
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={showRetired}
                onChange={(e) => setShowRetired(e.target.checked)}
                data-testid="show-retired"
              />
              {t("admin.alerts.showRetired")}
            </label>
          )}
        </div>
        <p className="text-xs text-muted-foreground">{t("admin.alerts.provisionalExplain")}</p>
        {view === "day" && <DayNotice day={day} />}
        <QueryStatus query={alerts} />
        {alerts.data?.length === 0 && (
          <Empty>{view === "open" ? t("admin.alerts.noneOpen") : t("admin.alerts.empty")}</Empty>
        )}
        <ul className="flex flex-col gap-2">
          {alerts.data?.map((a: AlertItem) => {
            const header = view === "open" && a.day !== lastDay ? a.day : null;
            lastDay = a.day;
            return (
              <li key={a.id} className="flex flex-col gap-2">
                {header && (
                  <h3 className="pt-2 text-sm font-semibold text-muted-foreground">
                    {formatDate(header)}
                    {!a.day_final && <span className="font-normal"> · {t("admin.alerts.dayHeaderProvisional")}</span>}
                  </h3>
                )}
                <div className="rounded-md border p-3 text-sm" data-testid="alert">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <p className="font-semibold">
                        {t("admin.alerts.rank", { rank: a.rank_in_day })} {a.user_name} ({t(`roles.${a.role}`)})
                      </p>
                      <p>
                        {t(`admin.alerts.action.${a.action}`)} {t(`resources.${a.resource}`)} · {formatTime(a.at)}
                      </p>
                      <p>
                        <Why code={a.template_code} strength={a.strength} />
                      </p>
                    </div>
                    <div className="flex flex-col items-end gap-1">
                      <StateBadge state={a.list_state} />
                      <Badge tone={statusTone(a.status)}>{t(`admin.alerts.status.${a.status}`)}</Badge>
                      <span className="text-xs text-muted-foreground">
                        {t("admin.alerts.score")} {a.score.toFixed(2)}
                      </span>
                    </div>
                  </div>
                  <ul className="mt-2 flex flex-wrap gap-1">
                    {a.top_features.slice(0, 3).map((f) => (
                      <li key={f.feature} className="rounded bg-muted px-2 py-0.5 text-xs">
                        {label(f.feature)}: {Number.isInteger(f.value) ? f.value : f.value.toFixed(2)}
                      </li>
                    ))}
                  </ul>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="mt-1 px-0 text-primary underline"
                    aria-expanded={selected === a.id}
                    onClick={() => {
                      setConfirmation(null);
                      setSelected(selected === a.id ? null : a.id);
                    }}
                  >
                    {selected === a.id ? t("admin.alerts.close") : t("admin.alerts.open")}
                  </Button>
                  {selected === a.id && (
                    <AlertDetail
                      id={a.id}
                      onReviewed={(outcome) => {
                        const text = t(`outcome.${outcome}`);
                        setConfirmation(
                          view === "open"
                            ? t("admin.alerts.reviewedMoved", { outcome: text })
                            : t("admin.alerts.reviewed", { outcome: text }),
                        );
                        if (view === "open") {
                          setSelected(null);
                        }
                      }}
                    />
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      </CardContent>
    </Card>
  );
}

function AlertDetail({ id, onReviewed }: { id: string; onReviewed: (outcome: Outcome) => void }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const label = useFeatureLabel();
  const [note, setNote] = useState("");
  // A review is written to the audit log and cannot be changed, so it is confirmed first.
  const [ask, setAsk] = useState<Outcome | null>(null);
  const detail = useQuery({
    queryKey: ["alert", id],
    queryFn: () => call(() => api.GET("/api/v1/alerts/{alert_id}", { params: { path: { alert_id: id } } })),
  });
  const review = useMutation({
    mutationFn: (outcome: Outcome) =>
      call(() =>
        api.POST("/api/v1/alerts/{alert_id}/review", {
          params: { path: { alert_id: id } },
          body: { outcome, note: note.trim() || null },
        }),
      ),
    onSuccess: (data, outcome) => {
      onReviewed(outcome);
      client.setQueryData(["alert", id], data);
      void client.invalidateQueries({ queryKey: ["alerts"] });
    },
  });
  if (detail.isError) {
    return <LoadError error={detail.error} onRetry={() => void detail.refetch()} />;
  }
  const d = detail.data;
  if (!d) {
    return <Loading />;
  }
  const fired = Object.entries(d.forgery_flags).filter(([, v]) => v === true);
  const last = d.reviews.at(-1);
  return (
    <div className="mt-2 flex flex-col gap-3 rounded-md bg-muted p-3" data-testid="alert-detail">
      <ConfirmDialog
        open={ask !== null}
        title={t("confirm.reviewTitle")}
        confirmLabel={t("confirm.recordReview")}
        destructive={ask === "misuse"}
        onCancel={() => setAsk(null)}
        onConfirm={() => {
          if (ask) {
            review.mutate(ask);
          }
          setAsk(null);
        }}
      >
        {ask && (
          <>
            <p className="font-semibold">{t("confirm.outcome", { outcome: t(`outcome.${ask}`) })}</p>
            <p>{t("confirm.patient", { name: `${d.patient_name} (${d.patient_mrn})` })}</p>
            <p>{note.trim() ? t("confirm.note", { note: note.trim() }) : t("confirm.noNote")}</p>
            <p className="text-destructive">{t("confirm.permanent")}</p>
          </>
        )}
      </ConfirmDialog>
      <p>
        <span className="font-semibold">{t("admin.alerts.patient")}:</span> {d.patient_name} ({d.patient_mrn})
      </p>
      <p>
        <span className="font-semibold">{t("admin.alerts.why")}:</span> <Why code={d.template_code} strength={d.strength} />
      </p>
      <div>
        <p className="font-semibold">{t("admin.alerts.topFeatures")}</p>
        <ul className="list-disc pl-5">
          {d.top_features.map((f) => (
            <li key={f.feature}>
              {label(f.feature)}: {Number.isInteger(f.value) ? f.value : f.value.toFixed(2)}
            </li>
          ))}
        </ul>
      </div>
      <p>
        <span className="font-semibold">{t("admin.alerts.flags")}:</span>{" "}
        {fired.length ? fired.map(([k]) => label(`flag_${k}`)).join(", ") : t("admin.alerts.noFlags")}
      </p>
      <div>
        <p className="font-semibold">{t("admin.alerts.timeline")}</p>
        <ol className="max-h-60 overflow-y-auto text-xs">
          {d.timeline.map((e, i) => (
            <li key={i} className={cn("flex flex-wrap gap-x-2 border-b py-1", e.is_this_alert && "font-semibold text-destructive")}>
              <span>{formatDateTime(e.at)}</span>
              <span>
                {t(`admin.alerts.action.${e.action}`)} {t(`resources.${e.resource}`)}
              </span>
              <span className="text-muted-foreground">#{e.patient_ref}</span>
              <span>{e.template_code ? t(`templates.${e.template_code}`) : t("admin.alerts.unexplained")}</span>
              {e.is_this_alert && <span>({t("admin.alerts.thisAccess")})</span>}
            </li>
          ))}
        </ol>
      </div>
      <p className="text-xs text-muted-foreground">
        {t("admin.alerts.scorer", { scorer: t(`admin.alerts.scorers.${d.scorer}`, { defaultValue: d.scorer }) })}
      </p>
      {last ? (
        <p role="status">{t("admin.alerts.reviewed", { outcome: t(`outcome.${last.outcome}`) })}</p>
      ) : (
        <div className="flex flex-col gap-2">
          <Textarea aria-label={t("admin.alerts.note")} placeholder={t("admin.alerts.note")} value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm">{t("admin.alerts.markAs")}</span>
            {OUTCOMES.map((o) => (
              <Button key={o} size="sm" variant={o === "misuse" ? "destructive" : "outline"} disabled={review.isPending} onClick={() => setAsk(o)}>
                {t(`outcome.${o}`)}
              </Button>
            ))}
          </div>
          {review.isError && <Notice tone="error">{errorMessage(review.error)}</Notice>}
        </div>
      )}
    </div>
  );
}
