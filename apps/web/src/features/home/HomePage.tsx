import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { fetchHealth } from "@/lib/api";

export function HomePage() {
  const { t, i18n } = useTranslation();
  const health = useQuery({
    queryKey: ["health"],
    queryFn: ({ signal }) => fetchHealth(signal),
    retry: 1,
  });

  return (
    <main className="mx-auto flex min-h-dvh max-w-md flex-col gap-4 p-4">
      <header className="flex items-center justify-between gap-2">
        <div>
          <h1 className="text-2xl font-bold text-primary">{t("app.name")}</h1>
          <p className="text-sm text-muted-foreground">{t("app.tagline")}</p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => void i18n.changeLanguage(i18n.language === "hi" ? "en" : "hi")}
        >
          {t("language.switch")}
        </Button>
      </header>

      <Card>
        <CardHeader>
          <CardTitle>{t("health.title")}</CardTitle>
        </CardHeader>
        <CardContent>
          {health.isPending && <p role="status">{t("health.checking")}</p>}
          {health.isSuccess && (
            <div role="status">
              <p className="font-medium text-success">{t("health.ok")}</p>
              <p className="text-sm text-muted-foreground">
                {t("health.version", { version: health.data.version })}
              </p>
            </div>
          )}
          {health.isError && (
            <div role="alert" className="flex flex-col items-start gap-2">
              <p className="font-medium text-destructive">{t("health.error")}</p>
              <Button size="sm" onClick={() => void health.refetch()}>
                {t("health.retry")}
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </main>
  );
}
