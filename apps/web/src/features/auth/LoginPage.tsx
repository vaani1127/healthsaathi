import { useMutation } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { AuthCard } from "@/features/auth/AuthCard";
import { Button } from "@/components/ui/button";
import { Alert, Field, Input } from "@/components/ui/form";
import { api, call } from "@/lib/api/client";
import { errorMessage } from "@/lib/format";
import { useAuth } from "@/stores/auth";

export function LoginPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const setChallenge = useAuth((s) => s.setChallenge);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const login = useMutation({
    mutationFn: () => call(() => api.POST("/api/v1/auth/login", { body: { email, password } })),
    onSuccess: async (res) => {
      setChallenge({ kind: res.status, token: res.challenge_token });
      await navigate({ to: res.status === "mfa_required" ? "/login/totp" : "/login/enrol" });
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    login.mutate();
  };

  return (
    <AuthCard title={t("auth.staffTitle")} subtitle={t("auth.staffSubtitle")}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field label={t("auth.email")} htmlFor="email">
          <Input
            id="email"
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        <Field label={t("auth.password")} htmlFor="password">
          <Input
            id="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        {login.isError && <Alert tone="error">{errorMessage(login.error)}</Alert>}
        <Button type="submit" disabled={login.isPending}>
          {t("auth.signIn")}
        </Button>
        <Link to="/patient/login" className="text-center text-sm text-primary underline">
          {t("auth.patientLink")}
        </Link>
      </form>
    </AuthCard>
  );
}
