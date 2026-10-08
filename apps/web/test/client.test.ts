import { describe, expect, it, vi } from "vitest";

import { ApiError, call, setRefresher } from "@/lib/api/client";
import { errorMessage } from "@/lib/format";

const res = (status: number) => new Response(null, { status });

describe("call", () => {
  it("refreshes once on 401 and retries", async () => {
    const refresh = vi.fn(() => Promise.resolve(true));
    setRefresher(refresh);
    const op = vi
      .fn()
      .mockResolvedValueOnce({ response: res(401), error: { code: "invalid-token" } })
      .mockResolvedValueOnce({ response: res(200), data: { ok: true } });
    await expect(call(op)).resolves.toEqual({ ok: true });
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(op).toHaveBeenCalledTimes(2);
  });

  it("turns problem responses into ApiError", async () => {
    setRefresher(() => Promise.resolve(false));
    const op = () =>
      Promise.resolve({ response: res(409), error: { code: "slot-taken", detail: "busy" } });
    const error = await call(op).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("slot-taken");
    expect(errorMessage(error)).toBe("The doctor already has an appointment at that time.");
  });

  it("reports network failures", async () => {
    const error = await call(() => Promise.reject(new TypeError("offline"))).catch((e: unknown) => e);
    expect((error as ApiError).code).toBe("network");
    expect((error as ApiError).status).toBe(0);
  });

  it("falls back to the server text for unknown codes", () => {
    expect(errorMessage(new ApiError({ status: 400, code: "brand-new", detail: "Server says" }))).toBe(
      "Server says",
    );
    expect(errorMessage(new Error("x"))).toBe("Something went wrong. Please try again.");
  });
});
