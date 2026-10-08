import createClient from "openapi-fetch";

import { API_URL } from "../config";
import { deviceId } from "../device";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type Role = Schemas["Role"];

export const api = createClient<paths>({ baseUrl: API_URL, credentials: "include" });

let accessToken: string | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

api.use({
  onRequest({ request }) {
    if (accessToken) {
      request.headers.set("authorization", `Bearer ${accessToken}`);
    }
    request.headers.set("x-device-id", deviceId());
    return request;
  },
});

export interface Problem {
  status: number;
  code: string;
  detail?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string | undefined;

  constructor(problem: Problem) {
    super(problem.detail ?? problem.code);
    this.status = problem.status;
    this.code = problem.code;
    this.detail = problem.detail;
  }
}

interface Result<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

type Refresher = () => Promise<boolean>;
let refresher: Refresher | null = null;

/** The auth store registers how to refresh the session; `call` uses it once on a 401. */
export function setRefresher(fn: Refresher): void {
  refresher = fn;
}

function toProblem(status: number, error: unknown): Problem {
  if (error && typeof error === "object" && "code" in error) {
    const e = error as { code: unknown; detail?: unknown };
    return {
      status,
      code: String(e.code),
      detail: typeof e.detail === "string" ? e.detail : undefined,
    };
  }
  return { status, code: status === 0 ? "network" : `http-${status}` };
}

/** Run a typed request, refreshing the access token once if it has expired. */
export async function call<T>(op: () => Promise<Result<T>>): Promise<T> {
  let result: Result<T>;
  try {
    result = await op();
    if (result.response.status === 401 && refresher && (await refresher())) {
      result = await op();
    }
  } catch {
    throw new ApiError({ status: 0, code: "network" });
  }
  if (!result.response.ok) {
    throw new ApiError(toProblem(result.response.status, result.error));
  }
  return result.data as T;
}
