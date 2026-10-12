import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Badge } from "@/components/ui/form";
import { QueryStatus } from "@/components/ui/state";
import { api, call, type Schemas } from "@/lib/api/client";
import { formatDateTime } from "@/lib/format";

type Backend = Schemas["BackendState"];

function Check({ ok, label, testId }: { ok: boolean | null | undefined; label: string; testId: string }) {
  const { t } = useTranslation();
  const tone = ok == null ? "default" : ok ? "success" : "danger";
  const word = ok == null ? t("verify.skipped") : ok ? t("verify.pass") : t("verify.fail");
  return (
    <div className="contents">
      <dt>{label}</dt>
      <dd>
        <Badge tone={tone} data-testid={testId}>
          {word}
        </Badge>
      </dd>
    </div>
  );
}

function short(ref: string): string {
  return ref.length > 18 ? `${ref.slice(0, 10)}...${ref.slice(-6)}` : ref;
}

function BackendRow({ backend }: { backend: Backend }) {
  const { t } = useTranslation();
  const name = t(`admin.audit.kind.${backend.kind}`);
  let badge: { tone: "default" | "success" | "warning" | "danger"; text: string };
  if (!backend.configured) {
    badge = { tone: "default", text: t("admin.audit.notConfigured") };
  } else if (backend.status === "confirmed") {
    badge = { tone: "success", text: t("admin.audit.status.confirmed") };
  } else if (backend.status === "failed") {
    badge = { tone: "danger", text: t("admin.audit.status.failed") };
  } else if (backend.status === "pending") {
    badge = { tone: "warning", text: t("admin.audit.status.pending") };
  } else {
    badge = { tone: "warning", text: t("admin.audit.waiting") };
  }
  return (
    <li className="flex flex-col gap-1 rounded-md border p-2" data-testid={`backend-${backend.key}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-medium">{name}</span>
        <Badge tone={badge.tone}>{badge.text}</Badge>
      </div>
      {!backend.configured && <p className="text-xs text-muted-foreground">{t(`admin.audit.notConfiguredWhy.${backend.key}`)}</p>}
      {backend.configured && backend.tx_ref && (
        <p className="break-all text-xs text-muted-foreground">
          {t(backend.key === "witness" ? "admin.audit.commit" : "admin.audit.tx")}: <span className="font-mono">{short(backend.tx_ref)}</span>
          {backend.block_ref && ` · ${t("admin.audit.block", { block: backend.block_ref })}`}
        </p>
      )}
      {backend.configured && backend.last_confirmed_at && (
        <p className="text-xs text-muted-foreground">
          {t("admin.audit.lastConfirmed", {
            at: formatDateTime(backend.last_confirmed_at),
            size: backend.last_confirmed_size ?? 0,
          })}
        </p>
      )}
      {backend.link && (
        <a className="text-sm text-primary underline" href={backend.link} target="_blank" rel="noreferrer">
          {backend.key === "witness" ? t("admin.audit.openCommit") : t("admin.audit.openTx")}
        </a>
      )}
    </li>
  );
}

/** SPEC 9 audit page: latest signed checkpoint, hash chain and consistency checks, and the real
 * state of every witness, including the ones that are not configured here. */
export function AuditStatus() {
  const { t } = useTranslation();
  const status = useQuery({
    queryKey: ["audit-status"],
    refetchInterval: 30_000,
    queryFn: () => call(() => api.GET("/api/v1/audit/status")),
  });
  const data = status.data;
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>{t("admin.audit.title")}</CardTitle>
          <Button size="sm" variant="outline" onClick={() => void status.refetch()} disabled={status.isFetching}>
            {t("admin.audit.refresh")}
          </Button>
        </div>
        <p className="text-sm text-muted-foreground">{t("admin.audit.explain")}</p>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        <QueryStatus query={status} />
        {data && (
          <>
            {data.problem && (
              <Alert tone="error" data-testid="audit-problem">
                <p>{data.chain_ok ? t("admin.audit.problemConsistency") : t("admin.audit.problemChain")}</p>
                <details className="mt-1 text-xs">
                  <summary>{t("admin.audit.technical")}</summary>
                  <span className="font-mono">{data.problem}</span>
                </details>
              </Alert>
            )}
            <dl className="grid grid-cols-[1fr_auto] gap-2 sm:grid-cols-2">
              <dt>{t("admin.audit.events")}</dt>
              <dd data-testid="audit-events">{data.events}</dd>
              <dt>{t("admin.audit.checkpoint")}</dt>
              <dd>
                {data.sth
                  ? t("admin.audit.checkpointOf", {
                      size: data.sth.tree_size,
                      at: formatDateTime(new Date(Number(data.sth.timestamp)).toISOString()),
                    })
                  : t("admin.audit.none")}
              </dd>
              <Check ok={data.chain_ok} label={t("admin.audit.chain")} testId="check-chain" />
              <Check ok={data.consistency_ok} label={t("admin.audit.consistency")} testId="check-consistency" />
            </dl>
            {data.sth && (
              <p className="break-all font-mono text-xs text-muted-foreground">
                {t("admin.audit.root")}: {String(data.sth.root_hex)}
              </p>
            )}
            <h3 className="font-semibold">{t("admin.audit.witnesses")}</h3>
            <ul className="flex flex-col gap-2">
              {data.backends.map((b) => (
                <BackendRow key={b.key} backend={b} />
              ))}
            </ul>
          </>
        )}
      </CardContent>
    </Card>
  );
}
