import { Link } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AuthCard } from "@/features/auth/AuthCard";

/** Shown for any address the app does not know, in the app's own frame and language. */
export function NotFoundPage() {
  const { t } = useTranslation();
  return (
    <AuthCard title={t("notFound.title")}>
      <div className="flex flex-col gap-4" data-testid="not-found">
        <p className="text-sm">{t("notFound.text")}</p>
        <Link to="/" className="text-center text-sm text-primary underline">
          {t("notFound.home")}
        </Link>
      </div>
    </AuthCard>
  );
}
