import i18n from "@/i18n";
import type { Schemas } from "@/lib/api/client";
import { formatDate, formatTime } from "@/lib/format";

type Entry = Schemas["AccessLogEntry"];

/** One plain sentence saying who opened what and why, in the reader's language. */
export function accessSentence(entry: Entry): string {
  const t = i18n.t.bind(i18n);
  const who = `${entry.user_name} (${t(`roles.${entry.role}`)})`;
  const what = t(`resources.${entry.resource}`);
  const did = t(`actions.${entry.action}`, { who, what });
  if (entry.decision === "deny") {
    return t("why.denied", { did });
  }
  const b = entry.because;
  const time = b.slot_start ? formatTime(b.slot_start) : "";
  switch (b.template) {
    case null:
    case undefined:
      return t("why.none", { did });
    case "T_APPT":
      return b.token_no
        ? t("why.appointmentToken", { did, token: b.token_no, time })
        : t("why.appointment", { did, time, date: b.slot_start ? formatDate(b.slot_start) : "" });
    case "T_QUEUE":
      return t("why.queue", { did, token: b.token_no ?? "" });
    case "T_LAB":
      return t("why.lab", { did, tests: (b.tests ?? []).join(", ") });
    case "T_REFERRAL":
      return t("why.referral", { did, from: b.from_name ?? "" });
    case "T_REASON":
      return t("why.reason", { did, reason: t(`reason.codes.${b.reason_code ?? "other"}`) });
    default:
      return t(`why.${b.template}`, { did });
  }
}
