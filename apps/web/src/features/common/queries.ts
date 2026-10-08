import { useQuery } from "@tanstack/react-query";

import { api, call } from "@/lib/api/client";

export function useDoctors() {
  return useQuery({
    queryKey: ["staff"],
    staleTime: 5 * 60_000,
    queryFn: () => call(() => api.GET("/api/v1/staff")),
    select: (staff) => staff.filter((s) => s.role === "doctor"),
  });
}

export function useStaffNames() {
  return useQuery({
    queryKey: ["staff"],
    staleTime: 5 * 60_000,
    queryFn: () => call(() => api.GET("/api/v1/staff")),
    select: (staff) => new Map(staff.map((s) => [s.user_id, s.name])),
  });
}

export function useQueue(doctorId?: string) {
  return useQuery({
    queryKey: ["queue", doctorId ?? "all"],
    queryFn: () =>
      call(() =>
        api.GET("/api/v1/queue", {
          params: { query: doctorId ? { doctor_user_id: doctorId } : {} },
        }),
      ),
  });
}

export function useChart(patientId: string) {
  return useQuery({
    queryKey: ["chart", patientId],
    retry: false,
    queryFn: () =>
      call(() => api.GET("/api/v1/patients/{patient_id}/chart", { params: { path: { patient_id: patientId } } })),
  });
}
