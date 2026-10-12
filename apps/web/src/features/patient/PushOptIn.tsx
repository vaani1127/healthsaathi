import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/form";
import { api, call } from "@/lib/api/client";
import { errorMessage } from "@/lib/format";

function supported(): boolean {
  return typeof window !== "undefined" && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

function keyBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const padded = (base64url + "=".repeat((4 - (base64url.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded);
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) {
    out[i] = raw.charCodeAt(i);
  }
  return out;
}

async function currentSubscription(): Promise<PushSubscription | null> {
  const registration = await navigator.serviceWorker.getRegistration();
  return registration ? registration.pushManager.getSubscription() : null;
}

/** Opt in to a notification each time someone at the clinic opens the patient's record
 * (SPEC 1.2 and 9). The notification says only that the record was opened. */
export function PushOptIn() {
  const { t } = useTranslation();
  const key = useQuery({
    queryKey: ["push-key"],
    staleTime: Infinity,
    enabled: supported(),
    queryFn: () => call(() => api.GET("/api/v1/push/key")),
  });
  const [subscribed, setSubscribed] = useState<boolean | null>(null);
  useEffect(() => {
    if (!supported()) {
      return;
    }
    void currentSubscription().then((s) => setSubscribed(s !== null));
  }, []);

  const toggle = useMutation({
    mutationFn: async (on: boolean) => {
      if (!on) {
        const existing = await currentSubscription();
        if (existing) {
          await call(() => api.POST("/api/v1/push/subscriptions/remove", { body: { endpoint: existing.endpoint } }));
          await existing.unsubscribe();
        }
        return false;
      }
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        throw new Error("denied");
      }
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: keyBytes(key.data?.public_key ?? ""),
      });
      const json = subscription.toJSON();
      await call(() =>
        api.POST("/api/v1/push/subscriptions", {
          body: {
            endpoint: subscription.endpoint,
            keys: { p256dh: json.keys?.p256dh ?? "", auth: json.keys?.auth ?? "" },
          },
        }),
      );
      return true;
    },
    onSuccess: setSubscribed,
  });

  if (!supported() || !key.data?.public_key || subscribed === null) {
    return null;
  }
  const denied = toggle.error instanceof Error && toggle.error.message === "denied";
  return (
    <div className="flex flex-col gap-2 rounded-md border p-3 text-sm">
      <p>{subscribed ? t("push.on") : t("push.explain")}</p>
      <div>
        <Button size="sm" variant={subscribed ? "outline" : "default"} disabled={toggle.isPending} onClick={() => toggle.mutate(!subscribed)}>
          {subscribed ? t("push.turnOff") : t("push.turnOn")}
        </Button>
      </div>
      {toggle.isError && <Alert tone="error">{denied ? t("push.denied") : errorMessage(toggle.error)}</Alert>}
    </div>
  );
}
