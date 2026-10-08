import i18n from "@/i18n";
import { ApiError } from "@/lib/api/client";

function locale(): string {
  return i18n.language === "hi" ? "hi-IN" : "en-IN";
}

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" });
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(locale(), { day: "numeric", month: "short", year: "numeric" });
}

export function formatDateTime(iso: string): string {
  return `${formatDate(iso)} ${formatTime(iso)}`;
}

export function formatMoney(paise: number): string {
  return new Intl.NumberFormat(locale(), { style: "currency", currency: "INR" }).format(paise / 100);
}

/** A translated message for an API problem, falling back to the server's own text. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const key = `errors.${error.code}`;
    if (i18n.exists(key)) {
      return i18n.t(key);
    }
    return error.detail ?? i18n.t("errors.generic");
  }
  return i18n.t("errors.generic");
}

/** Value for a datetime-local input, in the browser's time zone. */
export function toLocalInput(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
