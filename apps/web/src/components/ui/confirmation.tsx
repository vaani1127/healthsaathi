import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";

/** A page-level status line for the result of an action that removes an item from a list. It
 * sits outside the list, so it stays readable after the list refetches, until dismissed. The
 * live region is always rendered so screen readers announce the text when it appears. */
export function Confirmation({
  message,
  onDismiss,
  testId,
}: {
  message: string | null;
  onDismiss: () => void;
  testId: string;
}) {
  const { t } = useTranslation();
  return (
    <div aria-live="polite" role="status" data-testid={testId}>
      {message && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-success/40 bg-success/10 p-3 text-sm text-success">
          <span>{message}</span>
          <Button size="sm" variant="ghost" onClick={onDismiss}>
            {t("common.dismiss")}
          </Button>
        </div>
      )}
    </div>
  );
}
