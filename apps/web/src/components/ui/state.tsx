import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/form";
import { errorMessage } from "@/lib/format";

/** Shown while a list or record loads. */
export function Loading({ label }: { label?: string }) {
  const { t } = useTranslation();
  return (
    <p role="status" aria-live="polite" className="text-sm text-muted-foreground">
      {label ?? t("common.loading")}
    </p>
  );
}

/** Shown when a list has no rows. */
export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">{children}</p>;
}

/** A failed request, translated, with a retry button when the caller can retry. */
export function LoadError({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const { t } = useTranslation();
  return (
    <Alert tone="error" className="flex flex-wrap items-center justify-between gap-2">
      <span>{errorMessage(error)}</span>
      {onRetry && (
        <Button size="sm" variant="outline" onClick={onRetry}>
          {t("common.retry")}
        </Button>
      )}
    </Alert>
  );
}

interface QueryLike {
  isPending: boolean;
  isError: boolean;
  error: unknown;
  refetch: () => unknown;
}

/** Loading and error states for a query; renders nothing once data is there. */
export function QueryStatus({ query, label }: { query: QueryLike; label?: string }) {
  if (query.isError) {
    return <LoadError error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (query.isPending) {
    return <Loading {...(label ? { label } : {})} />;
  }
  return null;
}
