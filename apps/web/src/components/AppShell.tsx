import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useNavigate } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { api, call } from "@/lib/api/client";
import { useRealtime } from "@/lib/realtime";
import { homeFor, useAuth } from "@/stores/auth";

export function AppShell() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const token = useAuth((s) => s.token);
  const me = useAuth((s) => s.me);
  const logout = useAuth((s) => s.logout);
  useRealtime();

  const clinic = useQuery({
    queryKey: ["clinic", token?.clinic_id],
    enabled: Boolean(token?.clinic_id),
    staleTime: Infinity,
    queryFn: () => call(() => api.GET("/api/v1/clinic")),
  });

  return (
    <div className="min-h-dvh">
      <header className="sticky top-0 z-10 border-b bg-card/95 backdrop-blur print:hidden">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-2 px-4 py-2">
          <Link to={homeFor(token?.role)} className="font-bold text-primary">
            {t("app.name")}
          </Link>
          <span className="truncate text-sm text-muted-foreground">
            {clinic.data?.name}
            {token?.role && ` · ${t(`roles.${token.role}`)}`}
          </span>
          <div className="ml-auto flex items-center gap-1">
            <span className="hidden text-sm sm:inline">{me?.name}</span>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void i18n.changeLanguage(i18n.language === "hi" ? "en" : "hi")}
            >
              {t("language.switch")}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={async () => {
                await logout();
                await navigate({ to: "/login" });
              }}
            >
              {t("auth.signOut")}
            </Button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-5xl p-4">
        <Outlet />
      </main>
    </div>
  );
}

export function Placeholder({ title }: { title: string }) {
  const { t } = useTranslation();
  return (
    <section className="flex flex-col gap-2">
      <h1 className="text-xl font-semibold">{title}</h1>
      <p className="text-muted-foreground">{t("common.comingSoon")}</p>
    </section>
  );
}
