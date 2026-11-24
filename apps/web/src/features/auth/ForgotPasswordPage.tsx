import { useMutation } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Alert, Field, Input } from "@/components/ui/form";
import { AuthCard } from "@/features/auth/AuthCard";
import { api, call } from "@/lib/api/client";
import { errorMessage } from "@/lib/format";

/** Staff ask for a password reset link. The answer is the same whether or not the email has an
 * account, so the page always shows the same message after sending (ADR 0010). */
export function ForgotPasswordPage() {
  const { t } = useTranslation();
  const [email, setEmail] = useState("");

  const send = useMutation({
    mutationFn: () => call(() => api.POST("/api/v1/auth/password-reset/request", { body: { email } })),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    send.mutate();
  };

  return (
    <AuthCard title={t("reset.forgotTitle")} subtitle={t("reset.forgotSubtitle")}>
      {send.isSuccess ? (
        <div className="flex flex-col gap-4">
          <Alert tone="success">{t("reset.sent")}</Alert>
          <Link to="/login" className="text-center text-sm text-primary underline">
            {t("reset.backToSignIn")}
          </Link>
        </div>
      ) : (
        <form onSubmit={submit} className="flex flex-col gap-4">
          <Field label={t("auth.email")} htmlFor="reset-email">
            <Input
              id="reset-email"
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </Field>
          {send.isError && <Alert tone="error">{errorMessage(send.error)}</Alert>}
          <Button type="submit" disabled={send.isPending}>
            {t("reset.send")}
          </Button>
          <Link to="/login" className="text-center text-sm text-primary underline">
            {t("reset.backToSignIn")}
          </Link>
        </form>
      )}
    </AuthCard>
  );
}
