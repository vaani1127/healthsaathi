import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/form";
import { Empty, QueryStatus } from "@/components/ui/state";
import { api, call } from "@/lib/api/client";
import { formatDateTime } from "@/lib/format";

/** Local development only: emails the API would have sent, so sign-in codes can be read without a
 * mail server. The API answers 404 for this outside APP_ENV=local. */
export function DevMailboxPage() {
  const { t } = useTranslation();
  const [to, setTo] = useState("");
  const mails = useQuery({
    queryKey: ["dev-mailbox", to],
    refetchInterval: 3000,
    retry: false,
    queryFn: () => call(() => api.GET("/api/v1/dev/mailbox", { params: { query: to.trim() ? { to: to.trim() } : {} } })),
  });
  return (
    <main className="mx-auto flex max-w-3xl flex-col gap-4 p-4">
      <Card>
        <CardHeader>
          <CardTitle>{t("devMailbox.title")}</CardTitle>
          <p className="text-sm text-muted-foreground">{t("devMailbox.explain")}</p>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <Field label={t("devMailbox.filter")} htmlFor="mailbox-to">
            <Input id="mailbox-to" type="email" value={to} onChange={(e) => setTo(e.target.value)} />
          </Field>
          <QueryStatus query={mails} />
          {mails.data?.length === 0 && <Empty>{t("devMailbox.empty")}</Empty>}
          <ul className="flex flex-col gap-2">
            {mails.data?.map((m, i) => (
              <li key={`${m.sent_at ?? ""}-${i}`} className="rounded-md border p-3 text-sm" data-testid="mail">
                <p className="font-semibold">{m.subject}</p>
                <p className="text-xs text-muted-foreground">
                  {t("devMailbox.to", { to: m.to })}
                  {m.sent_at && ` · ${formatDateTime(m.sent_at)}`}
                </p>
                <p className="mt-1 whitespace-pre-wrap break-words">{m.text}</p>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </main>
  );
}
