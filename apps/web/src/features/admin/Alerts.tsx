import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert as Notice, Badge, Field, Input, Textarea } from "@/components/ui/form";
import { api, call, type Schemas } from "@/lib/api/client";
import { errorMessage, formatDateTime, formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";

type AlertItem = Schemas["AlertOut"];
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

export function Alerts() {
  const { t } = useTranslation();
  const [day, setDay] = useState(() => new Date().toISOString().slice(0, 10));
  const [selected, setSelected] = useState<string | null>(null);
  const alerts = useQuery({
    queryKey: ["alerts", day],
    queryFn: () => call(() => api.GET("/api/v1/alerts", { params: { query: { day } } })),
  });
  const label = useFeatureLabel();
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("admin.alerts.title")}</CardTitle>
        <p className="text-sm text-muted-foreground">{t("admin.alerts.explain")}</p>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Field label={t("admin.day")} htmlFor="alerts-day">
          <Input id="alerts-day" type="date" value={day} onChange={(e) => setDay(e.target.value)} />
        </Field>
        {alerts.isError && <Notice tone="error">{errorMessage(alerts.error)}</Notice>}
        {alerts.data?.length === 0 && <p className="text-sm text-muted-foreground">{t("admin.alerts.empty")}</p>}
        <ul className="flex flex-col gap-2">
          {alerts.data?.map((a: AlertItem) => (
            <li key={a.id} className="rounded-md border p-3 text-sm" data-testid="alert">
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
                onClick={() => setSelected(selected === a.id ? null : a.id)}
              >
                {selected === a.id ? t("admin.alerts.close") : t("admin.alerts.open")}
              </Button>
              {selected === a.id && <AlertDetail id={a.id} day={day} />}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function AlertDetail({ id, day }: { id: string; day: string }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const label = useFeatureLabel();
  const [note, setNote] = useState("");
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
    onSuccess: (data) => {
      client.setQueryData(["alert", id], data);
      void client.invalidateQueries({ queryKey: ["alerts", day] });
    },
  });
  if (detail.isError) {
    return <Notice tone="error">{errorMessage(detail.error)}</Notice>;
  }
  const d = detail.data;
  if (!d) {
    return null;
  }
  const fired = Object.entries(d.forgery_flags).filter(([, v]) => v === true);
  const last = d.reviews.at(-1);
  return (
    <div className="mt-2 flex flex-col gap-3 rounded-md bg-muted p-3" data-testid="alert-detail">
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
      <p className="text-xs text-muted-foreground">{t("admin.alerts.scorer", { scorer: d.scorer })}</p>
      {last ? (
        <p role="status">{t("admin.alerts.reviewed", { outcome: t(`outcome.${last.outcome}`) })}</p>
      ) : (
        <div className="flex flex-col gap-2">
          <Textarea aria-label={t("admin.alerts.note")} placeholder={t("admin.alerts.note")} value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm">{t("admin.alerts.markAs")}</span>
            {OUTCOMES.map((o) => (
              <Button key={o} size="sm" variant={o === "misuse" ? "destructive" : "outline"} disabled={review.isPending} onClick={() => review.mutate(o)}>
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
