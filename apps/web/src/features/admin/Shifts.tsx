import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Field, Input, Select } from "@/components/ui/form";
import { Empty, QueryStatus } from "@/components/ui/state";
import { api, call, type Schemas } from "@/lib/api/client";
import { errorMessage, formatDate, formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const DAY = 86_400_000;

function localDate(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Staff shifts for the next 7 days (SPEC 1.2 clinic ops); shifts are workflow evidence too. */
export function Shifts() {
  const { t } = useTranslation();
  const client = useQueryClient();
  const [from] = useState(() => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return d;
  });
  const until = new Date(from.getTime() + 7 * DAY);
  const shifts = useQuery({
    queryKey: ["shifts", from.toISOString()],
    queryFn: () =>
      call(() => api.GET("/api/v1/shifts", { params: { query: { start: from.toISOString(), end: until.toISOString() } } })),
  });
  const staff = useQuery({ queryKey: ["staff"], queryFn: () => call(() => api.GET("/api/v1/staff")) });
  const names = new Map(staff.data?.map((s) => [s.user_id, s.name]));
  const [form, setForm] = useState({ member: "", day: localDate(new Date()), start: "09:00", end: "17:00" });
  const [formError, setFormError] = useState<string | null>(null);

  const refresh = () => client.invalidateQueries({ queryKey: ["shifts"] });
  const add = useMutation({
    mutationFn: (body: Schemas["ShiftCreate"]) => call(() => api.POST("/api/v1/shifts", { body })),
    onSuccess: refresh,
  });
  const cancel = useMutation({
    mutationFn: (id: string) => call(() => api.POST("/api/v1/shifts/{shift_id}/cancel", { params: { path: { shift_id: id } } })),
    onSuccess: refresh,
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const person = staff.data?.find((s) => `${s.user_id}:${s.role}` === form.member);
    const starts = new Date(`${form.day}T${form.start}`);
    let ends = new Date(`${form.day}T${form.end}`);
    if (ends <= starts) {
      // A night shift ends the next morning.
      ends = new Date(ends.getTime() + DAY);
    }
    if (!person) {
      setFormError(t("admin.shiftChooseStaff"));
      return;
    }
    setFormError(null);
    add.mutate({ user_id: person.user_id, role: person.role, starts_at: starts.toISOString(), ends_at: ends.toISOString() });
  };

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>{t("admin.shifts")}</CardTitle>
          <p className="text-sm text-muted-foreground">{t("admin.shiftsExplain")}</p>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          <QueryStatus query={shifts} />
          {cancel.isError && <Alert tone="error">{errorMessage(cancel.error)}</Alert>}
          {shifts.data?.length === 0 && <Empty>{t("admin.noShifts")}</Empty>}
          <ul className="divide-y text-sm">
            {shifts.data?.map((s) => (
              <li key={s.id} className="flex items-center justify-between gap-2 py-1.5">
                <span className={cn(s.status === "cancelled" && "text-muted-foreground line-through")}>
                  {formatDate(s.starts_at)} {formatTime(s.starts_at)} {t("admin.until")} {formatTime(s.ends_at)} ·{" "}
                  {names.get(s.user_id) ?? t("common.unknownStaff")} · {t(`roles.${s.role}`)}
                </span>
                {s.status === "scheduled" && (
                  <Button size="sm" variant="ghost" disabled={cancel.isPending} onClick={() => cancel.mutate(s.id)}>
                    {t("admin.cancelShift")}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>{t("admin.addShift")}</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="grid grid-cols-2 gap-3">
            <Field label={t("admin.staffMember")} htmlFor="shift-member" className="col-span-2" error={formError ?? undefined}>
              <Select id="shift-member" value={form.member} onChange={(e) => setForm({ ...form, member: e.target.value })}>
                <option value="">{t("common.choose")}</option>
                {staff.data
                  ?.filter((s) => s.role !== "patient")
                  .map((s) => (
                    <option key={`${s.user_id}:${s.role}`} value={`${s.user_id}:${s.role}`}>
                      {s.name} · {t(`roles.${s.role}`)}
                    </option>
                  ))}
              </Select>
            </Field>
            <Field label={t("admin.shiftDay")} htmlFor="shift-day" className="col-span-2">
              <Input id="shift-day" type="date" required value={form.day} onChange={(e) => setForm({ ...form, day: e.target.value })} />
            </Field>
            <Field label={t("admin.from")} htmlFor="shift-start">
              <Input id="shift-start" type="time" required value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} />
            </Field>
            <Field label={t("admin.to")} htmlFor="shift-end">
              <Input id="shift-end" type="time" required value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} />
            </Field>
            {add.isError && (
              <Alert tone="error" className="col-span-2">
                {errorMessage(add.error)}
              </Alert>
            )}
            {add.isSuccess && (
              <Alert tone="success" className="col-span-2">
                {t("admin.shiftAdded")}
              </Alert>
            )}
            <Button type="submit" className="col-span-2" disabled={add.isPending}>
              {t("admin.add")}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
