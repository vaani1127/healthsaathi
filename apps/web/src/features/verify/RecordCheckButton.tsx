import type { RecordCheckResponse } from "@healthsaathi/receipt-verify";
import { useMutation } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/form";
import { canCheck, checkRecord, downloadRecordCheck, type RecordType } from "@/features/verify/recordCheck";
import { RecordResultView } from "@/features/verify/RecordResultView";
import { api, call } from "@/lib/api/client";
import { errorMessage } from "@/lib/format";
import { useAuth } from "@/stores/auth";

/**
 * "Check this record": asks the API for the record's committed versions and proofs, then checks
 * them in this browser (ADR 0011). Shown only to roles the access policy lets view the record.
 */
export function RecordCheckButton({ type, id, label }: { type: RecordType; id: string; label?: string }) {
  const { t } = useTranslation();
  const role = useAuth((s) => s.token?.role);
  const check = useMutation({
    mutationFn: async () => {
      const response = (await call(() =>
        api.GET("/api/v1/records/{record_type}/{record_id}/integrity", {
          params: { path: { record_type: type, record_id: id } },
        }),
      )) as unknown as RecordCheckResponse;
      return checkRecord(response);
    },
  });
  if (!canCheck(role, type)) {
    return null;
  }
  return (
    <div className="flex flex-col gap-2" data-testid={`record-check-${type}`}>
      {!check.data && (
        <Button
          variant="ghost"
          size="sm"
          className="self-start px-0 text-primary underline"
          disabled={check.isPending}
          onClick={() => check.mutate()}
        >
          {check.isPending ? t("record.checking") : (label ?? t("record.checkThis"))}
        </Button>
      )}
      {check.isError && <Alert tone="error">{errorMessage(check.error)}</Alert>}
      {check.data && (
        <div className="flex flex-col gap-2 rounded-md bg-muted p-2">
          <RecordResultView result={check.data} />
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" onClick={() => downloadRecordCheck(check.data.response)}>
              {t("record.download")}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => check.mutate()} disabled={check.isPending}>
              {t("record.again")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
