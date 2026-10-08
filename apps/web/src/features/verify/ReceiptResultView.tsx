import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/form";
import type { ReceiptResult } from "@/features/verify/check";
import { CHAIN } from "@/lib/config";

function Line({ ok, label }: { ok: boolean | null; label: string }) {
  const { t } = useTranslation();
  const tone = ok === null ? "default" : ok ? "success" : "danger";
  const word = ok === null ? t("verify.skipped") : ok ? t("verify.pass") : t("verify.fail");
  return (
    <li className="flex items-center justify-between gap-2">
      <span>{label}</span>
      <Badge tone={tone}>{word}</Badge>
    </li>
  );
}

export function ReceiptResultView({ result }: { result: ReceiptResult }) {
  const { t } = useTranslation();
  const { offline, anchor } = result;
  const anchored = anchor === null ? null : anchor.status === "anchored";
  const verified = offline.ok && anchored !== false && anchor?.keyRegistered !== false;
  return (
    <div className="flex flex-col gap-2 text-sm" data-testid="receipt-result">
      <p className="font-semibold" role="status">
        {verified ? t("verify.verified") : t("verify.notVerified")}
      </p>
      <ul className="flex flex-col gap-1">
        <Line ok={offline.payload} label={t("verify.checkPayload")} />
        <Line ok={offline.inclusion} label={t("verify.checkInclusion")} />
        <Line ok={offline.signature && offline.clinic} label={t("verify.checkSignature")} />
        <Line ok={anchored} label={t("verify.checkChain")} />
        {anchor?.keyRegistered != null && <Line ok={anchor.keyRegistered} label={t("verify.checkKey")} />}
      </ul>
      {anchor === null && !result.anchorError && (
        <p className="text-xs text-muted-foreground">{t("verify.noChain")}</p>
      )}
      {result.anchorError && <p className="text-xs text-muted-foreground">{t("verify.chainUnreachable")}</p>}
      {anchor?.status === "not_anchored" && (
        <p className="text-xs text-muted-foreground">{t("verify.notAnchoredYet")}</p>
      )}
      {anchor?.status === "mismatch" && <p className="text-xs text-destructive">{t("verify.mismatch")}</p>}
      {anchor?.txHash && CHAIN.explorerUrl && (
        <a
          className="text-xs text-primary underline"
          href={`${CHAIN.explorerUrl.replace(/\/$/, "")}/tx/${anchor.txHash}`}
          target="_blank"
          rel="noreferrer"
        >
          {t("verify.viewTx")}
        </a>
      )}
    </div>
  );
}
