import { useIsFetching, useIsMutating } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

/** A thin bar at the top of every page while any request is running for more than a moment,
 * so a slow network (or a database far away) never makes the app look frozen. */
export function GlobalBusy() {
  const { t } = useTranslation();
  const busy = useIsFetching() + useIsMutating() > 0;
  const [waited, setWaited] = useState(false);
  useEffect(() => {
    if (!busy) {
      return;
    }
    const timer = setTimeout(() => setWaited(true), 300);
    return () => {
      clearTimeout(timer);
      setWaited(false);
    };
  }, [busy]);
  if (!busy || !waited) {
    return null;
  }
  return (
    <div
      role="progressbar"
      aria-label={t("common.working")}
      aria-busy="true"
      className="busy-bar fixed inset-x-0 top-0 z-50 h-1 overflow-hidden bg-primary/20 print:hidden"
    >
      <div className="busy-bar-fill h-full w-1/3 bg-primary" />
    </div>
  );
}
