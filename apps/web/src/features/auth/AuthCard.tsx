import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function AuthCard({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
}) {
  const { t, i18n } = useTranslation();
  return (
    <main className="mx-auto flex min-h-dvh max-w-sm flex-col justify-center gap-4 p-4">
      <div className="flex items-center justify-between">
        <span className="text-xl font-bold text-primary">{t("app.name")}</span>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => void i18n.changeLanguage(i18n.language === "hi" ? "en" : "hi")}
        >
          {t("language.switch")}
        </Button>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>{title}</CardTitle>
          {subtitle && <p className="text-sm text-muted-foreground">{subtitle}</p>}
        </CardHeader>
        <CardContent>{children}</CardContent>
      </Card>
    </main>
  );
}
