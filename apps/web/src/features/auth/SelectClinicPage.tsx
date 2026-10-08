import { useMutation } from "@tanstack/react-query";
import { Navigate, useNavigate } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AuthCard } from "@/features/auth/AuthCard";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/form";
import type { Role } from "@/lib/api/client";
import { errorMessage } from "@/lib/format";
import { homeFor, useAuth } from "@/stores/auth";

export function SelectClinicPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const me = useAuth((s) => s.me);
  const selectClinic = useAuth((s) => s.selectClinic);

  const choose = useMutation({
    mutationFn: async ({ clinicId, role }: { clinicId: string; role: Role }) => {
      await selectClinic(clinicId, role);
      return homeFor(role);
    },
    onSuccess: (to) => navigate({ to }),
  });

  if (!me) {
    return <Navigate to="/login" />;
  }

  return (
    <AuthCard title={t("auth.selectClinic")}>
      <div className="flex flex-col gap-2">
        {me.memberships.length === 0 && <Alert>{t("auth.noClinics")}</Alert>}
        {me.memberships.map((m) => (
          <Button
            key={`${m.clinic_id}-${m.role}`}
            variant="outline"
            className="h-auto justify-between py-3"
            onClick={() => choose.mutate({ clinicId: m.clinic_id, role: m.role })}
          >
            <span>{m.clinic_name}</span>
            <span className="text-muted-foreground">{t(`roles.${m.role}`)}</span>
          </Button>
        ))}
        {choose.isError && <Alert tone="error">{errorMessage(choose.error)}</Alert>}
      </div>
    </AuthCard>
  );
}
