import type { VersionCheck } from "@healthsaathi/receipt-verify";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/form";
import type { RecordResult } from "@/features/verify/recordCheck";
import { CHAIN } from "@/lib/config";
import { formatDateTime } from "@/lib/format";

function Mark({ ok, label }: { ok: boolean | null; label: string }) {
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

function VersionRow({ v, committedAt }: { v: VersionCheck; committedAt: string | null }) {
  const { t } = useTranslation();
  return (
    <li className="flex flex-col gap-1 rounded-md border p-2" data-testid="record-version">
      <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span>{t("record.version", { version: v.version })}</span>
        {committedAt && <span>{t("record.committedAt", { at: formatDateTime(committedAt) })}</span>}
        {v.current ? <Badge>{t("record.current")}</Badge> : <Badge>{t("record.older")}</Badge>}
        {v.backfilled && <Badge tone="warning">{t("record.backfilledBadge")}</Badge>}
      </p>
      <ul className="flex flex-col gap-1">
        <Mark ok={v.leaf} label={t("record.checkLeaf")} />
        <Mark ok={v.inclusion} label={t("record.checkInclusion")} />
        <Mark ok={v.signature} label={t("record.checkSignature")} />
        {v.current && <Mark ok={v.commitment} label={t("record.checkContent")} />}
      </ul>
    </li>
  );
}

/** The browser's own verdict on a record check: computed from the proofs, never the server's. */
export function RecordResultView({ result }: { result: RecordResult }) {
  const { t } = useTranslation();
  const { response, offline, anchor, verdict } = result;
  const committedAt = new Map(response.versions.map((v) => [`${v.record_id}:${v.version}`, v.committed_at]));
  const tone = verdict === "verified" ? "text-success" : verdict === "failed" ? "text-destructive" : "";
  return (
    <div className="flex flex-col gap-2 text-sm" data-testid="record-result" data-verdict={verdict}>
      <p className={`font-semibold ${tone}`} role="status">
        {t(`record.verdict.${verdict}`)}
      </p>
      <p className="text-xs text-muted-foreground">{t("record.browserOnly")}</p>
      {offline.backfilled && (
        <p className="rounded-md bg-warning/10 p-2 text-xs" data-testid="record-backfilled">
          {t("record.backfilled", {
            at: offline.coveredSince ? formatDateTime(offline.coveredSince) : t("record.unknownTime"),
          })}
        </p>
      )}
      {!offline.backfilled && offline.coveredSince && (
        <p className="text-xs text-muted-foreground">
          {t("record.coveredSince", { at: formatDateTime(offline.coveredSince) })}
        </p>
      )}
      {verdict === "pending" && <p className="text-xs text-muted-foreground">{t("record.pendingExplain")}</p>}
      {verdict === "not_committed" && <p className="text-xs text-muted-foreground">{t("record.notCommittedExplain")}</p>}
      {response.versions.length > 0 && (
        <ul className="flex flex-col gap-2">
          {offline.versions.map((v) => (
            <VersionRow key={`${v.record_id}:${v.version}`} v={v} committedAt={committedAt.get(`${v.record_id}:${v.version}`) ?? null} />
          ))}
        </ul>
      )}
      <ul className="flex flex-col gap-1">
        <Mark
          ok={anchor === null ? null : anchor.status === "anchored" ? true : anchor.status === "mismatch" ? false : null}
          label={t("verify.checkChain")}
        />
        {anchor?.keyRegistered != null && <Mark ok={anchor.keyRegistered} label={t("verify.checkKey")} />}
      </ul>
      {anchor === null && !result.anchorError && offline.verdict === "verified" && (
        <p className="text-xs text-muted-foreground">{t("verify.noChain")}</p>
      )}
      {result.anchorError && <p className="text-xs text-muted-foreground">{t("verify.chainUnreachable")}</p>}
      {anchor?.status === "not_anchored" && <p className="text-xs text-muted-foreground">{t("verify.notAnchoredYet")}</p>}
      {anchor?.status === "mismatch" && <p className="text-xs text-destructive">{t("verify.mismatch")}</p>}
      {anchor?.txHash && CHAIN.explorerUrl && (
        <a
          className="text-xs text-primary underline"
          href={`${CHAIN.explorerUrl.replace(/\/$/, "")}/tx/${anchor.txHash}`}
          target="_blank"
          rel="noreferrer"
          data-testid="record-tx-link"
        >
          {t("verify.viewTx")}
        </a>
      )}
    </div>
  );
}
