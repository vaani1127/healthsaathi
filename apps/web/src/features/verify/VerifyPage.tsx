import { useMutation } from "@tanstack/react-query";
import { type ChangeEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Field, Textarea } from "@/components/ui/form";
import { checkReceipt, parseReceipt, type ReceiptResult } from "@/features/verify/check";
import { ReceiptResultView } from "@/features/verify/ReceiptResultView";
import { checkRecord, parseRecordCheck, type RecordResult } from "@/features/verify/recordCheck";
import { RecordResultView } from "@/features/verify/RecordResultView";

type Checked = { kind: "receipt"; result: ReceiptResult } | { kind: "record"; result: RecordResult };

/** Public page: anyone holding a receipt or a record check file can check it, without signing in. */
export function VerifyPage() {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const [invalid, setInvalid] = useState(false);
  const check = useMutation({
    mutationFn: async (raw: string): Promise<Checked> => {
      const record = parseRecordCheck(raw);
      if (record !== null) {
        return { kind: "record", result: await checkRecord(record) };
      }
      const receipt = parseReceipt(raw);
      if (receipt === null) {
        throw new Error("invalid");
      }
      return { kind: "receipt", result: await checkReceipt(receipt) };
    },
    onMutate: () => setInvalid(false),
    onError: () => setInvalid(true),
  });

  const onFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) {
      setText(await file.text());
    }
  };

  return (
    <main className="mx-auto flex max-w-xl flex-col gap-4 p-4">
      <Card>
        <CardHeader>
          <CardTitle>{t("verify.title")}</CardTitle>
          <p className="text-sm text-muted-foreground">{t("verify.explain")}</p>
          <p className="text-sm text-muted-foreground">{t("verify.explainRecord")}</p>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <Field label={t("verify.file")} htmlFor="receipt-file">
            <input id="receipt-file" type="file" accept="application/json,.json" onChange={onFile} />
          </Field>
          <Field label={t("verify.paste")} htmlFor="receipt-text">
            <Textarea
              id="receipt-text"
              rows={6}
              className="font-mono text-xs"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
          </Field>
          <Button disabled={!text.trim() || check.isPending} onClick={() => check.mutate(text)}>
            {t("verify.check")}
          </Button>
          {invalid && <Alert tone="error">{t("verify.invalid")}</Alert>}
          {check.data?.kind === "receipt" && <ReceiptResultView result={check.data.result} />}
          {check.data?.kind === "record" && <RecordResultView result={check.data.result} />}
        </CardContent>
      </Card>
    </main>
  );
}
