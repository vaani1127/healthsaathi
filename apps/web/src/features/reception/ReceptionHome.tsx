import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Badge, Input } from "@/components/ui/form";
import { RegisterPatient } from "@/features/reception/RegisterPatient";
import { api, call } from "@/lib/api/client";
import { errorMessage, formatTime } from "@/lib/format";

export function ReceptionHome() {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [registering, setRegistering] = useState(false);

  const search = useQuery({
    queryKey: ["patients", query],
    enabled: query.trim().length >= 2,
    queryFn: () => call(() => api.GET("/api/v1/patients", { params: { query: { q: query.trim() } } })),
  });

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>{t("reception.findPatient")}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <Input
            aria-label={t("reception.searchLabel")}
            placeholder={t("reception.searchPlaceholder")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {search.isError && <Alert tone="error">{errorMessage(search.error)}</Alert>}
          <ul className="flex flex-col divide-y">
            {search.data?.items.map((p) => (
              <li key={p.id} className="py-2">
                <Link
                  to="/reception/patients/$patientId"
                  params={{ patientId: p.id }}
                  className="flex justify-between gap-2 hover:underline"
                >
                  <span>{p.name}</span>
                  <span className="text-sm text-muted-foreground">{p.mrn}</span>
                </Link>
              </li>
            ))}
            {search.data && search.data.items.length === 0 && (
              <li className="py-2 text-sm text-muted-foreground">{t("reception.noMatches")}</li>
            )}
          </ul>
          {registering ? (
            <RegisterPatient onCancel={() => setRegistering(false)} />
          ) : (
            <Button variant="secondary" onClick={() => setRegistering(true)}>
              {t("reception.registerNew")}
            </Button>
          )}
        </CardContent>
      </Card>
      <TodayAppointments />
    </div>
  );
}

function TodayAppointments() {
  const { t } = useTranslation();
  const client = useQueryClient();
  const appointments = useQuery({
    queryKey: ["appointments", "today"],
    queryFn: () => call(() => api.GET("/api/v1/appointments")),
  });
  const queue = useQuery({ queryKey: ["queue", "all"], queryFn: () => call(() => api.GET("/api/v1/queue")) });
  const tokens = new Map((queue.data ?? []).map((q) => [q.appointment_id, q.token_no]));

  const issue = useMutation({
    mutationFn: (appointmentId: string) =>
      call(() => api.POST("/api/v1/queue/tokens", { body: { appointment_id: appointmentId } })),
    onSuccess: () => client.invalidateQueries({ queryKey: ["queue"] }),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("reception.today")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {issue.isError && <Alert tone="error">{errorMessage(issue.error)}</Alert>}
        {appointments.data?.length === 0 && (
          <p className="text-sm text-muted-foreground">{t("reception.noAppointments")}</p>
        )}
        <ul className="flex flex-col divide-y">
          {appointments.data?.map((a) => {
            const token = tokens.get(a.id);
            return (
              <li key={a.id} className="flex items-center justify-between gap-2 py-2">
                <div className="flex flex-col">
                  <span>{a.patient.name}</span>
                  <span className="text-xs text-muted-foreground">
                    {formatTime(a.slot_start)} · {t(`appointmentStatus.${a.status}`)}
                  </span>
                </div>
                {token !== undefined ? (
                  <Badge tone="success">{t("reception.tokenNo", { no: token })}</Badge>
                ) : (
                  a.status === "booked" && (
                    <Button size="sm" onClick={() => issue.mutate(a.id)} disabled={issue.isPending}>
                      {t("reception.issueToken")}
                    </Button>
                  )
                )}
              </li>
            );
          })}
        </ul>
      </CardContent>
    </Card>
  );
}
