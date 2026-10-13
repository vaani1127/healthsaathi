import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Badge, Field } from "@/components/ui/form";
import { PasswordInput } from "@/components/ui/password";
import { Empty, QueryStatus } from "@/components/ui/state";
import { api, call } from "@/lib/api/client";
import { errorMessage, formatDateTime } from "@/lib/format";
import { useAuth } from "@/stores/auth";

/** Every signed-in device of the current user, with sign out per device (SPEC 1.2). */
export function SessionsPage() {
  const role = useAuth((s) => s.token?.role);
  return (
    <div className="flex flex-col gap-4">
      <Sessions />
      {role !== "patient" && <ChangePassword />}
    </div>
  );
}

function Sessions() {
  const { t } = useTranslation();
  const client = useQueryClient();
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: () => call(() => api.GET("/api/v1/auth/sessions")) });
  const revoke = useMutation({
    mutationFn: (id: string) =>
      call(() => api.DELETE("/api/v1/auth/sessions/{session_id}", { params: { path: { session_id: id } } })),
    onSuccess: () => client.invalidateQueries({ queryKey: ["sessions"] }),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("sessions.title")}</CardTitle>
        <p className="text-sm text-muted-foreground">{t("sessions.explain")}</p>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <QueryStatus query={sessions} />
        {revoke.isError && <Alert tone="error">{errorMessage(revoke.error)}</Alert>}
        {sessions.data?.length === 0 && <Empty>{t("sessions.none")}</Empty>}
        <ul className="flex flex-col divide-y">
          {sessions.data?.map((s) => (
            <li key={s.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
              <div className="flex min-w-0 flex-col">
                <span className="truncate font-medium">{s.user_agent ?? t("sessions.unknownDevice")}</span>
                <span className="text-xs text-muted-foreground">
                  {t("sessions.started", { at: formatDateTime(s.started_at) })} ·{" "}
                  {t("sessions.lastUsed", { at: formatDateTime(s.last_used_at) })}
                </span>
              </div>
              {s.current ? (
                <Badge tone="success">{t("sessions.current")}</Badge>
              ) : (
                <Button size="sm" variant="outline" onClick={() => revoke.mutate(s.id)} disabled={revoke.isPending}>
                  {t("sessions.revoke")}
                </Button>
              )}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

const MIN_LENGTH = 10;

/** Change the password with the current one (ASVS 2.1.5, 2.1.6). Other devices are signed out. */
export function ChangePassword() {
  const { t } = useTranslation();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [touched, setTouched] = useState(false);
  const change = useMutation({
    mutationFn: () =>
      call(() => api.POST("/api/v1/auth/password", { body: { current_password: current, new_password: next } })),
    onSuccess: () => {
      setCurrent("");
      setNext("");
      setTouched(false);
    },
  });
  const tooShort = next.length < MIN_LENGTH;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (!tooShort && current) {
      change.mutate();
    }
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("password.title")}</CardTitle>
        <p className="text-sm text-muted-foreground">{t("password.explain")}</p>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} noValidate className="flex flex-col gap-3">
          <Field label={t("password.current")} htmlFor="current-password">
            <PasswordInput
              id="current-password"
              autoComplete="current-password"
              required
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
            />
          </Field>
          <Field
            label={t("password.new")}
            htmlFor="new-password"
            hint={t("invite.rules", { n: MIN_LENGTH })}
            error={touched && tooShort ? t("invite.tooShort", { n: MIN_LENGTH }) : undefined}
          >
            <PasswordInput
              id="new-password"
              autoComplete="new-password"
              required
              value={next}
              onChange={(e) => setNext(e.target.value)}
            />
          </Field>
          {change.isError && <Alert tone="error">{errorMessage(change.error)}</Alert>}
          {change.isSuccess && <Alert tone="success">{t("password.changed")}</Alert>}
          <Button type="submit" disabled={change.isPending}>
            {t("password.save")}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
