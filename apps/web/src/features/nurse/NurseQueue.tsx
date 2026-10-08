import { Link } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Badge } from "@/components/ui/form";
import { useQueue } from "@/features/common/queries";
import { errorMessage } from "@/lib/format";

export function QueueList({ doctorId, to }: { doctorId?: string; to: "/nurse/patients/$patientId" | "/doctor/patients/$patientId" }) {
  const { t } = useTranslation();
  const queue = useQueue(doctorId);
  if (queue.isError) {
    return <Alert tone="error">{errorMessage(queue.error)}</Alert>;
  }
  const items = queue.data ?? [];
  return (
    <ul className="flex flex-col divide-y" aria-label={t("queue.title")}>
      {queue.isPending && <li role="status">{t("common.loading")}</li>}
      {!queue.isPending && items.length === 0 && (
        <li className="py-2 text-sm text-muted-foreground">{t("queue.empty")}</li>
      )}
      {items.map((q) => (
        <li key={q.id}>
          <Link
            to={to}
            params={{ patientId: q.patient.id }}
            search={{ appointment: q.appointment_id, token: q.id }}
            className="flex items-center justify-between gap-2 py-3 hover:bg-muted"
          >
            <span className="flex items-center gap-3">
              <span className="w-10 text-center text-lg font-bold" aria-label={t("queue.tokenLabel")}>
                {q.token_no}
              </span>
              <span>{q.patient.name}</span>
            </span>
            <Badge tone={q.status === "done" ? "success" : q.status === "waiting" ? "warning" : "default"}>
              {t(`queueStatus.${q.status}`)}
            </Badge>
          </Link>
        </li>
      ))}
    </ul>
  );
}

export function NurseQueue() {
  const { t } = useTranslation();
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("queue.today")}</CardTitle>
      </CardHeader>
      <CardContent>
        <QueueList to="/nurse/patients/$patientId" />
      </CardContent>
    </Card>
  );
}
