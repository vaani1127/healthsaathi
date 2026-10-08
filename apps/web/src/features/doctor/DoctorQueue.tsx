import { useTranslation } from "react-i18next";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { QueueList } from "@/features/nurse/NurseQueue";
import { useAuth } from "@/stores/auth";

export function DoctorQueue() {
  const { t } = useTranslation();
  const me = useAuth((s) => s.me);
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("queue.mine")}</CardTitle>
      </CardHeader>
      <CardContent>{me && <QueueList doctorId={me.user_id} to="/doctor/patients/$patientId" />}</CardContent>
    </Card>
  );
}
