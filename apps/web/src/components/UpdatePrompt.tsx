import { useTranslation } from "react-i18next";
import { useRegisterSW } from "virtual:pwa-register/react";

import { Button } from "@/components/ui/button";

export function UpdatePrompt() {
  const { t } = useTranslation();
  const {
    needRefresh: [needRefresh],
    updateServiceWorker,
  } = useRegisterSW();

  if (!needRefresh) {
    return null;
  }
  return (
    <div
      role="status"
      className="fixed inset-x-4 bottom-4 mx-auto flex max-w-md items-center justify-between gap-2 rounded-lg border bg-card p-3 shadow-lg"
    >
      <span className="text-sm">{t("pwa.update")}</span>
      <Button size="sm" onClick={() => void updateServiceWorker(true)}>
        {t("pwa.reload")}
      </Button>
    </div>
  );
}
