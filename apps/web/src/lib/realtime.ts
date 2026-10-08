import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { API_URL } from "@/lib/config";
import { useAuth } from "@/stores/auth";

const QUERY_KEYS: Record<string, string[][]> = {
  "queue.changed": [["queue"]],
  "appointments.changed": [["appointments"], ["queue"]],
  "lab.changed": [["worklist"], ["chart"]],
  "break_glass.created": [["break-glass"]],
  "alerts.new": [["alerts"]],
};

/** Keeps lists fresh: the server says what changed, and the matching queries are refetched. */
export function useRealtime(): void {
  const client = useQueryClient();
  const token = useAuth((s) => s.token?.access_token);
  const scoped = useAuth((s) => Boolean(s.token?.clinic_id));

  useEffect(() => {
    if (!token || !scoped) {
      return;
    }
    let socket: WebSocket | null = null;
    let stopped = false;
    let delay = 1000;

    const connect = () => {
      socket = new WebSocket(`${API_URL.replace(/^http/, "ws")}/api/v1/ws`);
      socket.onopen = () => {
        socket?.send(JSON.stringify({ type: "auth", token: useAuth.getState().token?.access_token }));
        delay = 1000;
      };
      socket.onmessage = (event: MessageEvent<string>) => {
        try {
          const message = JSON.parse(event.data) as { type: string };
          for (const key of QUERY_KEYS[message.type] ?? []) {
            void client.invalidateQueries({ queryKey: key });
          }
        } catch {
          // "pong" and other plain messages
        }
      };
      socket.onclose = () => {
        if (!stopped) {
          setTimeout(connect, delay);
          delay = Math.min(delay * 2, 30000);
        }
      };
    };
    connect();
    const ping = setInterval(() => socket?.readyState === WebSocket.OPEN && socket.send("ping"), 25000);
    return () => {
      stopped = true;
      clearInterval(ping);
      socket?.close();
    };
  }, [client, token, scoped]);
}
