import { useMutation } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api/client";

export function ResultValues({ values }: { values: Record<string, unknown> }) {
  const entries = Object.entries(values);
  if (entries.length === 0) {
    return null;
  }
  return (
    <dl className="grid grid-cols-2 gap-x-2" data-testid="result-values">
      {entries.map(([key, raw]) => {
        const v = raw as { value?: unknown; unit?: unknown };
        return (
          <div key={key} className="contents">
            <dt className="text-muted-foreground">{key}</dt>
            <dd>
              {String(v.value ?? raw)} {v.unit ? String(v.unit) : ""}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

export function DocumentLink({ id, label }: { id: string; label: string }) {
  const { t } = useTranslation();
  const download = useMutation({
    mutationFn: async () => {
      const { data, response } = await api.GET("/api/v1/documents/{document_id}", {
        params: { path: { document_id: id } },
        parseAs: "blob",
      });
      if (!response.ok || !data) {
        throw new Error("download failed");
      }
      const url = URL.createObjectURL(data);
      window.open(url, "_blank", "noopener");
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    },
  });
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <Button
        variant="ghost"
        size="sm"
        className="px-0 text-primary underline"
        disabled={download.isPending}
        onClick={() => download.mutate()}
      >
        {label}
      </Button>
      {download.isError && (
        <span role="alert" className="text-sm text-destructive">
          {t("portal.downloadFailed")}
        </span>
      )}
    </span>
  );
}

