import { useQuery } from "@tanstack/react-query";

import { api, call } from "@/lib/api/client";
import { useAuth } from "@/stores/auth";

/** The calendar date (YYYY-MM-DD) in `timeZone`. Alerts, revenue and schedules are dated in the
 * clinic's timezone, which need not be the device's: between 18:30 and 24:00 UTC a device on UTC
 * is still on the previous day in India. */
export function clinicDay(timeZone: string | undefined, at: Date = new Date()): string {
  if (timeZone) {
    try {
      // en-CA formats dates as YYYY-MM-DD.
      return new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(at);
    } catch {
      // An unknown timezone name falls back to the device's date.
    }
  }
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`;
}

/** Today in the signed-in clinic's timezone; the device's date until the clinic has loaded. */
export function useClinicToday(): string {
  const clinicId = useAuth((s) => s.token?.clinic_id);
  const clinic = useQuery({
    queryKey: ["clinic", clinicId],
    enabled: Boolean(clinicId),
    staleTime: Infinity,
    queryFn: () => call(() => api.GET("/api/v1/clinic")),
  });
  return clinicDay(clinic.data?.timezone);
}
