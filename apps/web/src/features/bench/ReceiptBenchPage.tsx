import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { createPublicClient, http } from "viem";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Field, Input } from "@/components/ui/form";
import {
  type BenchFile,
  type BenchResult,
  deviceInfo,
  downloadResult,
  timeChain,
  timeOffline,
} from "@/features/bench/bench";
import { CHAIN } from "@/lib/config";

const OFFLINE_RUNS = 30;
const CHAIN_RUNS = 10;

/** Public page for the anchoring study (SPEC 8): how long receipt checks take on this device. */
export function ReceiptBenchPage() {
  const { t } = useTranslation();
  const [label, setLabel] = useState("");
  const [step, setStep] = useState("");
  const run = useMutation({
    mutationFn: async (): Promise<BenchResult> => {
      const resp = await fetch("/bench/receipts.json", { cache: "no-store" });
      if (!resp.ok) {
        throw new Error("bench file missing");
      }
      const bench = (await resp.json()) as BenchFile;
      const result: BenchResult = {
        label: label.trim(),
        ...deviceInfo(),
        started_at: new Date().toISOString(),
        offline: [],
        chain: null,
      };
      for (const { tree_size, receipt } of bench.receipts) {
        setStep(t("bench.stepOffline", { size: tree_size.toLocaleString() }));
        // Let the page paint between sizes.
        await new Promise((r) => setTimeout(r, 0));
        const timing = timeOffline(receipt, OFFLINE_RUNS);
        result.offline.push({ tree_size, proof_hashes: receipt.proof.length, ...timing });
      }
      const first = bench.receipts[0]?.receipt;
      if (bench.chain_probe && first && CHAIN.rpcUrl && CHAIN.auditAnchor) {
        setStep(t("bench.stepChain"));
        const client = createPublicClient({ transport: http(CHAIN.rpcUrl) });
        result.chain = await timeChain(
          client,
          first,
          bench.chain_probe,
          { auditAnchor: CHAIN.auditAnchor, clinicRegistry: CHAIN.clinicRegistry ?? undefined },
          CHAIN_RUNS,
        );
      }
      setStep("");
      return result;
    },
  });

  return (
    <main className="mx-auto flex max-w-xl flex-col gap-4 p-4">
      <Card>
        <CardHeader>
          <CardTitle>{t("bench.title")}</CardTitle>
          <p className="text-sm text-muted-foreground">{t("bench.explain")}</p>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <Field label={t("bench.device")} htmlFor="bench-label">
            <Input id="bench-label" value={label} onChange={(e) => setLabel(e.target.value)} />
          </Field>
          <Button disabled={run.isPending} onClick={() => run.mutate()}>
            {run.isPending ? t("bench.running") : t("bench.run")}
          </Button>
          {step && <p className="text-sm text-muted-foreground" role="status">{step}</p>}
          {run.isError && <Alert tone="error">{t("bench.failed")}</Alert>}
          {run.data && (
            <>
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left">
                    <th className="py-1">{t("bench.treeSize")}</th>
                    <th className="py-1 text-right">{t("bench.median")}</th>
                    <th className="py-1 text-right">{t("bench.p95")}</th>
                  </tr>
                </thead>
                <tbody>
                  {run.data.offline.map((r) => (
                    <tr key={r.tree_size} className="border-t">
                      <td className="py-1">{r.tree_size.toLocaleString()}</td>
                      <td className="py-1 text-right">{r.median_ms.toFixed(2)}</td>
                      <td className="py-1 text-right">{r.p95_ms.toFixed(2)}</td>
                    </tr>
                  ))}
                  {run.data.chain && (
                    <tr className="border-t">
                      <td className="py-1">{t("bench.chainReads")}</td>
                      <td className="py-1 text-right">{run.data.chain.median_ms.toFixed(0)}</td>
                      <td className="py-1 text-right">{run.data.chain.p95_ms.toFixed(0)}</td>
                    </tr>
                  )}
                </tbody>
              </table>
              {run.data.offline.some((r) => !r.verified) && <Alert tone="error">{t("bench.notVerified")}</Alert>}
              <Button variant="outline" onClick={() => downloadResult(run.data)}>
                {t("bench.download")}
              </Button>
              <pre className="hidden" data-testid="bench-result">
                {JSON.stringify(run.data)}
              </pre>
            </>
          )}
        </CardContent>
      </Card>
    </main>
  );
}
