import { useTranslation } from "react-i18next";

import type { Schemas } from "@/lib/api/client";
import { formatDateTime } from "@/lib/format";

export function VitalsList({ vitals }: { vitals: Schemas["VitalOut"][] }) {
  const { t } = useTranslation();
  if (vitals.length === 0) {
    return <p className="text-sm text-muted-foreground">{t("vitals.none")}</p>;
  }
  return (
    <ul className="flex flex-col divide-y text-sm" data-testid="vitals-list">
      {vitals.map((v) => (
        <li key={v.id} className="py-2">
          <p className="text-xs text-muted-foreground">{formatDateTime(v.recorded_at)}</p>
          <p>
            {v.bp_sys !== null && v.bp_dia !== null && `${t("vitals.bp")} ${v.bp_sys}/${v.bp_dia} · `}
            {v.pulse !== null && `${t("vitals.pulse")} ${v.pulse} · `}
            {v.temp_c !== null && `${t("vitals.temp_c")} ${v.temp_c} · `}
            {v.spo2 !== null && `SpO2 ${v.spo2}%`}
          </p>
        </li>
      ))}
    </ul>
  );
}
