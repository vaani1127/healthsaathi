import { useMutation } from "@tanstack/react-query";
import { Navigate, useNavigate } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { AuthCard } from "@/features/auth/AuthCard";
import { finishSignIn } from "@/features/auth/finish";
import { Button } from "@/components/ui/button";
import { Alert, Field, Input } from "@/components/ui/form";
import { api, call } from "@/lib/api/client";
import { errorMessage } from "@/lib/format";
import { useAuth } from "@/stores/auth";

export function TotpPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const challenge = useAuth((s) => s.challenge);
  const [code, setCode] = useState("");

  const verify = useMutation({
    mutationFn: async () => {
      const token = await call(() =>
        api.POST("/api/v1/auth/totp/verify", {
          body: { challenge_token: challenge?.token ?? "", code },
        }),
      );
      return finishSignIn(token);
    },
    onSuccess: (to) => navigate({ to }),
  });

  if (!challenge || challenge.kind !== "mfa_required") {
    return <Navigate to="/login" />;
  }

  const submit = (e: FormEvent) => {
    e.preventDefault();
    verify.mutate();
  };

  return (
    <AuthCard title={t("auth.totpTitle")} subtitle={t("auth.totpSubtitle")}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field label={t("auth.code")} htmlFor="code">
          <Input
            id="code"
            inputMode="numeric"
            autoComplete="one-time-code"
            pattern="[0-9]{6}"
            maxLength={6}
            required
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
          />
        </Field>
        {verify.isError && <Alert tone="error">{errorMessage(verify.error)}</Alert>}
        <Button type="submit" disabled={verify.isPending || code.length !== 6}>
          {t("auth.verify")}
        </Button>
      </form>
    </AuthCard>
  );
}
