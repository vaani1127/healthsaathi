import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Badge, Field, Select, Textarea } from "@/components/ui/form";
import { VitalsList } from "@/features/common/VitalsList";
import { api, call, type Schemas } from "@/lib/api/client";
import { errorMessage, formatTime } from "@/lib/format";

type Code = Schemas["BreakGlassIn"]["reason_code"];
const CODES: Code[] = ["emergency", "unconscious", "severe_allergy", "system_outage", "other"];

export function BreakGlass({ patientId }: { patientId: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [code, setCode] = useState<Code>("emergency");
  const [text, setText] = useState("");
  const start = useMutation({
    mutationFn: () =>
      call(() =>
        api.POST("/api/v1/patients/{patient_id}/break-glass", {
          params: { path: { patient_id: patientId } },
          body: { reason_code: code, reason_text: text },
        }),
      ),
  });

  if (start.data) {
    const view = start.data;
    return (
      <Card className="border-destructive">
        <CardHeader>
          <CardTitle>{t("breakGlass.viewTitle", { name: view.patient.name })}</CardTitle>
          <p className="text-sm text-destructive">{t("breakGlass.until", { time: formatTime(view.expires_at) })}</p>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 text-sm">
          <div>
            <h3 className="font-semibold">{t("allergy.title")}</h3>
            {view.allergies.map((a) => (
              <Badge key={a.id} tone="danger" className="mr-1">
                {a.substance}
              </Badge>
            ))}
          </div>
          <div>
            <h3 className="font-semibold">{t("conditions.title")}</h3>
            <ul>
              {view.conditions.map((c) => (
                <li key={c.id}>{c.text}</li>
              ))}
            </ul>
          </div>
          <div>
            <h3 className="font-semibold">{t("breakGlass.medicines")}</h3>
            <ul>
              {view.current_medicines.map((m, i) => (
                <li key={i}>{String(m.drug ?? "")} {String(m.dose ?? "")}</li>
              ))}
            </ul>
          </div>
          <VitalsList vitals={view.last_vitals ? [view.last_vitals] : []} />
        </CardContent>
      </Card>
    );
  }

  const submit = (e: FormEvent) => {
    e.preventDefault();
    start.mutate();
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("breakGlass.title")}</CardTitle>
        <p className="text-sm text-muted-foreground">{t("breakGlass.explain")}</p>
      </CardHeader>
      <CardContent>
        {!open ? (
          <Button variant="destructive" onClick={() => setOpen(true)}>
            {t("breakGlass.start")}
          </Button>
        ) : (
          <form onSubmit={submit} className="flex flex-col gap-3">
            <Field label={t("reason.code")} htmlFor="bg-code">
              <Select id="bg-code" value={code} onChange={(e) => setCode(e.target.value as Code)}>
                {CODES.map((c) => (
                  <option key={c} value={c}>
                    {t(`breakGlass.codes.${c}`)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label={t("reason.text")} htmlFor="bg-text">
              <Textarea id="bg-text" required minLength={5} value={text} onChange={(e) => setText(e.target.value)} />
            </Field>
            {start.isError && <Alert tone="error">{errorMessage(start.error)}</Alert>}
            <Button type="submit" variant="destructive" disabled={start.isPending || text.trim().length < 5}>
              {t("breakGlass.confirm")}
            </Button>
          </form>
        )}
      </CardContent>
    </Card>
  );
}
