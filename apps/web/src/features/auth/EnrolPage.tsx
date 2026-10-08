import { useMutation, useQuery } from "@tanstack/react-query";
import { Navigate, useNavigate } from "@tanstack/react-router";
import QRCode from "qrcode";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { AuthCard } from "@/features/auth/AuthCard";
import { finishSignIn } from "@/features/auth/finish";
import { Button } from "@/components/ui/button";
import { Alert, Field, Input } from "@/components/ui/form";
import { api, call } from "@/lib/api/client";
import { errorMessage } from "@/lib/format";
import { useAuth } from "@/stores/auth";

export function EnrolPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const challenge = useAuth((s) => s.challenge);
  const [code, setCode] = useState("");
  const enrollToken = challenge?.kind === "mfa_enrollment_required" ? challenge.token : null;

  const setup = useQuery({
    queryKey: ["totp-enroll", enrollToken],
    enabled: enrollToken !== null,
    staleTime: Infinity,
    retry: false,
    queryFn: async () => {
      const res = await call(() =>
        api.POST("/api/v1/auth/totp/enroll", { body: { enroll_token: enrollToken ?? "" } }),
      );
      return { ...res, qr: await QRCode.toDataURL(res.otpauth_uri, { margin: 1, width: 220 }) };
    },
  });

  const activate = useMutation({
    mutationFn: async () => {
      const token = await call(() =>
        api.POST("/api/v1/auth/totp/activate", { body: { enroll_token: enrollToken ?? "", code } }),
      );
      return finishSignIn(token);
    },
    onSuccess: (to) => navigate({ to }),
  });

  if (enrollToken === null) {
    return <Navigate to="/login" />;
  }

  const submit = (e: FormEvent) => {
    e.preventDefault();
    activate.mutate();
  };

  return (
    <AuthCard title={t("auth.enrolTitle")} subtitle={t("auth.enrolSubtitle")}>
      {setup.isError && <Alert tone="error">{errorMessage(setup.error)}</Alert>}
      {setup.data && (
        <form onSubmit={submit} className="flex flex-col items-center gap-4">
          <img src={setup.data.qr} alt={t("auth.qrAlt")} width={220} height={220} />
          <p className="text-center text-sm text-muted-foreground">{t("auth.enrolManual")}</p>
          <code data-testid="totp-secret" className="break-all rounded bg-muted px-2 py-1 text-sm">
            {setup.data.secret}
          </code>
          <Field label={t("auth.code")} htmlFor="code" className="w-full">
            <Input
              id="code"
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              required
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
            />
          </Field>
          {activate.isError && <Alert tone="error">{errorMessage(activate.error)}</Alert>}
          <Button type="submit" className="w-full" disabled={activate.isPending || code.length !== 6}>
            {t("auth.activate")}
          </Button>
        </form>
      )}
    </AuthCard>
  );
}
