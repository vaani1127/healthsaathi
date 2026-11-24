import { useMutation } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Alert, Field, Input } from "@/components/ui/form";
import { PasswordInput } from "@/components/ui/password";
import { AuthCard } from "@/features/auth/AuthCard";
import { readResetLink } from "@/features/auth/resetLink";
import { api, ApiError, call } from "@/lib/api/client";
import { errorMessage } from "@/lib/format";

const MIN_LENGTH = 10;

/** Staff set a new password from the reset link, with their authenticator code if they use one. */
export function ResetPasswordPage() {
  const { t } = useTranslation();
  // Read once, then take the token out of the address bar and the history entry.
  const [link] = useState(() => readResetLink(window.location.hash));
  useEffect(() => {
    if (window.location.hash) {
      window.history.replaceState(window.history.state, "", window.location.pathname);
    }
  }, []);

  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [code, setCode] = useState("");
  const [touched, setTouched] = useState(false);

  const reset = useMutation({
    mutationFn: () =>
      call(() =>
        api.POST("/api/v1/auth/password-reset/confirm", {
          body: { email: link.email, token: link.token, password, code: code.trim() || null },
        }),
      ),
  });

  const tooShort = password.length < MIN_LENGTH;
  const mismatch = repeat !== password;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (!tooShort && !mismatch) {
      reset.mutate();
    }
  };

  const failed = reset.error instanceof ApiError ? reset.error.code : null;
  const askAgain = (
    <Link to="/forgot-password" className="text-center text-sm text-primary underline">
      {t("reset.askAgain")}
    </Link>
  );

  if (!link.email || !link.token) {
    return (
      <AuthCard title={t("reset.title")}>
        <div className="flex flex-col gap-4">
          <Alert tone="error">{t("reset.noLink")}</Alert>
          {askAgain}
        </div>
      </AuthCard>
    );
  }
  if (reset.isSuccess) {
    return (
      <AuthCard title={t("reset.title")}>
        <div className="flex flex-col gap-4">
          <Alert tone="success">{t("reset.done")}</Alert>
          <Link to="/login" className="text-center text-sm text-primary underline">
            {t("invite.toSignIn")}
          </Link>
        </div>
      </AuthCard>
    );
  }
  return (
    <AuthCard title={t("reset.title")} subtitle={t("reset.subtitle", { email: link.email })}>
      <form onSubmit={submit} noValidate className="flex flex-col gap-4">
        <Field
          label={t("auth.password")}
          htmlFor="reset-password"
          hint={t("invite.rules", { n: MIN_LENGTH })}
          error={touched && tooShort ? t("invite.tooShort", { n: MIN_LENGTH }) : undefined}
        >
          <PasswordInput
            id="reset-password"
            autoComplete="new-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <Field
          label={t("invite.repeat")}
          htmlFor="reset-repeat"
          error={touched && mismatch ? t("invite.mismatch") : undefined}
        >
          <PasswordInput
            id="reset-repeat"
            autoComplete="new-password"
            required
            value={repeat}
            onChange={(e) => setRepeat(e.target.value)}
          />
        </Field>
        <Field
          label={t("reset.code")}
          htmlFor="reset-code"
          hint={t("reset.codeHint")}
          error={failed === "authenticator-code-required" ? t("errors.authenticator-code-required") : undefined}
        >
          <Input
            id="reset-code"
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={8}
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
        </Field>
        {reset.isError && failed !== "authenticator-code-required" && (
          <Alert tone="error">{errorMessage(reset.error)}</Alert>
        )}
        {failed === "invalid-reset-link" && askAgain}
        <Button type="submit" disabled={reset.isPending}>
          {t("reset.submit")}
        </Button>
      </form>
    </AuthCard>
  );
}
