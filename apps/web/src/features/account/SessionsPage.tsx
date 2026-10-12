import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Badge } from "@/components/ui/form";
import { Empty, QueryStatus } from "@/components/ui/state";
import { api, call } from "@/lib/api/client";
import { errorMessage, formatDateTime } from "@/lib/format";

/** Every signed-in device of the current user, with sign out per device (SPEC 1.2). */
export function SessionsPage() {
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
