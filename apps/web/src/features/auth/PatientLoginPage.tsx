import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Alert, Field, Input } from "@/components/ui/form";
import { AuthCard } from "@/features/auth/AuthCard";
import { finishSignIn } from "@/features/auth/finish";
import { api, call } from "@/lib/api/client";
import { errorMessage } from "@/lib/format";

export function PatientLoginPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);

  const request = useMutation({
    mutationFn: () => call(() => api.POST("/api/v1/auth/otp/request", { body: { email } })),
    onSuccess: () => setSent(true),
  });
  const verify = useMutation({
    mutationFn: async () => {
      const token = await call(() => api.POST("/api/v1/auth/otp/verify", { body: { email, code } }));
      return finishSignIn(token);
    },
    onSuccess: (to) => navigate({ to }),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (sent) {
      verify.mutate();
    } else {
      request.mutate();
    }
  };

  return (
    <AuthCard title={t("portal.signInTitle")} subtitle={t("portal.signInSubtitle")}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field label={t("auth.email")} htmlFor="p-email">
          <Input
            id="p-email"
            type="email"
            autoComplete="email"
            required
            disabled={sent}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        {sent && (
          <>
            <Alert>{t("portal.codeSent")}</Alert>
            <Field label={t("auth.code")} htmlFor="p-code">
              <Input
                id="p-code"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                required
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
              />
            </Field>
          </>
        )}
        {(request.isError || verify.isError) && (
          <Alert tone="error">{errorMessage(request.error ?? verify.error)}</Alert>
        )}
        <Button type="submit" disabled={request.isPending || verify.isPending || (sent && code.length !== 6)}>
          {sent ? t("auth.signIn") : t("portal.sendCode")}
        </Button>
        {sent && (
          <Button type="button" variant="ghost" onClick={() => request.mutate()}>
            {t("portal.resend")}
          </Button>
        )}
      </form>
    </AuthCard>
  );
}
