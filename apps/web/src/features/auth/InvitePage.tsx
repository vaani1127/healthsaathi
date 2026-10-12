import { useMutation } from "@tanstack/react-query";
import { Link, useSearch } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Alert, Field, Input } from "@/components/ui/form";
import { AuthCard } from "@/features/auth/AuthCard";
import { api, call } from "@/lib/api/client";
import { errorMessage } from "@/lib/format";

const MIN_LENGTH = 10;

/** Invited staff set their password here from the link in the invite email. */
export function InvitePage() {
  const { t } = useTranslation();
  const { email, token } = useSearch({ from: "/invite" });
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [touched, setTouched] = useState(false);

  const accept = useMutation({
    mutationFn: () => call(() => api.POST("/api/v1/auth/invite/accept", { body: { email, token, password } })),
  });

  const tooShort = password.length < MIN_LENGTH;
  const mismatch = repeat !== password;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (!tooShort && !mismatch) {
      accept.mutate();
    }
  };

  if (!email || !token) {
    return (
      <AuthCard title={t("invite.title")}>
        <Alert tone="error">{t("errors.invalid-invite")}</Alert>
      </AuthCard>
    );
  }
  if (accept.isSuccess) {
    return (
      <AuthCard title={t("invite.title")}>
        <div className="flex flex-col gap-4">
          <Alert tone="success">{t("invite.done")}</Alert>
          <Link to="/login" className="text-center text-sm text-primary underline">
            {t("invite.toSignIn")}
          </Link>
        </div>
      </AuthCard>
    );
  }
  return (
    <AuthCard title={t("invite.title")} subtitle={t("invite.subtitle", { email })}>
      <form onSubmit={submit} noValidate className="flex flex-col gap-4">
        <Field
          label={t("auth.password")}
          htmlFor="new-password"
          hint={t("invite.rules", { n: MIN_LENGTH })}
          error={touched && tooShort ? t("invite.tooShort", { n: MIN_LENGTH }) : undefined}
        >
          <Input
            id="new-password"
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <Field
          label={t("invite.repeat")}
          htmlFor="repeat-password"
          error={touched && mismatch ? t("invite.mismatch") : undefined}
        >
          <Input
            id="repeat-password"
            type="password"
            autoComplete="new-password"
            required
            value={repeat}
            onChange={(e) => setRepeat(e.target.value)}
          />
        </Field>
        {accept.isError && <Alert tone="error">{errorMessage(accept.error)}</Alert>}
        <Button type="submit" disabled={accept.isPending}>
          {t("invite.submit")}
        </Button>
      </form>
    </AuthCard>
  );
}
