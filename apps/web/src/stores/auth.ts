import { create } from "zustand";

import { api, call, type Role, type Schemas, setAccessToken, setRefresher } from "@/lib/api/client";
import { dropAllDrafts } from "@/lib/drafts";

type Token = Schemas["TokenResponse"];
type Me = Schemas["MeResponse"];

export interface Challenge {
  kind: "mfa_required" | "mfa_enrollment_required";
  token: string;
}

interface Scope {
  clinicId: string;
  role: Role;
}

const SCOPE_KEY = "hs.scope";

function readScope(): Scope | null {
  try {
    const raw = localStorage.getItem(SCOPE_KEY);
    return raw ? (JSON.parse(raw) as Scope) : null;
  } catch {
    return null;
  }
}

function writeScope(scope: Scope | null): void {
  try {
    if (scope) {
      localStorage.setItem(SCOPE_KEY, JSON.stringify(scope));
    } else {
      localStorage.removeItem(SCOPE_KEY);
    }
  } catch {
    // Without storage the user picks the clinic again after a reload.
  }
}

interface AuthState {
  token: Token | null;
  me: Me | null;
  challenge: Challenge | null;
  ready: boolean;
  setChallenge: (challenge: Challenge | null) => void;
  acceptToken: (token: Token) => Promise<void>;
  selectClinic: (clinicId: string, role: Role) => Promise<void>;
  restore: () => Promise<void>;
  refresh: () => Promise<boolean>;
  logout: () => Promise<void>;
}

export const useAuth = create<AuthState>((set, get) => ({
  token: null,
  me: null,
  challenge: null,
  ready: false,

  setChallenge: (challenge) => set({ challenge }),

  acceptToken: async (token) => {
    setAccessToken(token.access_token);
    set({ token, challenge: null });
    const me = await call(() => api.GET("/api/v1/auth/me"));
    set({ me });
    if (token.clinic_id && token.role) {
      writeScope({ clinicId: token.clinic_id, role: token.role });
    }
  },

  selectClinic: async (clinicId, role) => {
    const token = await call(() =>
      api.POST("/api/v1/auth/select-clinic", { body: { clinic_id: clinicId, role } }),
    );
    await get().acceptToken(token);
  },

  refresh: async () => {
    const scope = readScope();
    const { data, response } = await api.POST("/api/v1/auth/refresh", {
      body: scope ? { clinic_id: scope.clinicId, role: scope.role } : {},
    });
    if (!response.ok || !data) {
      setAccessToken(null);
      set({ token: null, me: null });
      return false;
    }
    setAccessToken(data.access_token);
    set({ token: data });
    return true;
  },

  restore: async () => {
    try {
      if (await get().refresh()) {
        const me = await call(() => api.GET("/api/v1/auth/me"));
        set({ me });
      }
    } catch {
      set({ token: null, me: null });
    } finally {
      set({ ready: true });
    }
  },

  logout: async () => {
    try {
      await api.POST("/api/v1/auth/logout");
    } finally {
      setAccessToken(null);
      writeScope(null);
      set({ token: null, me: null, challenge: null });
      await clearOfflineData();
    }
  },
}));

setRefresher(() => useAuth.getState().refresh());

/** Remove cached API responses and unsent drafts when someone signs out. */
async function clearOfflineData(): Promise<void> {
  try {
    if ("caches" in window) {
      for (const name of await caches.keys()) {
        if (name.startsWith("hs-api")) {
          await caches.delete(name);
        }
      }
    }
    await dropAllDrafts();
  } catch {
    // Nothing cached or storage blocked.
  }
}

export function homeFor(role: Role | null | undefined): string {
  switch (role) {
    case "reception":
      return "/reception";
    case "nurse":
      return "/nurse";
    case "doctor":
      return "/doctor";
    case "lab_tech":
      return "/lab";
    case "clinic_admin":
      return "/admin";
    case "patient":
      return "/patient";
    default:
      return "/select-clinic";
  }
}
