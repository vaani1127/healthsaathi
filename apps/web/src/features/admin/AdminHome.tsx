import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Badge, Field, Input, Select, Textarea } from "@/components/ui/form";
import { api, call, type Role, type Schemas } from "@/lib/api/client";
import { errorMessage, formatDate, formatDateTime, formatMoney } from "@/lib/format";
import { cn } from "@/lib/utils";

const TABS = ["staff", "schedules", "services", "consent", "breakGlass", "reports", "revenue", "audit"] as const;
type Tab = (typeof TABS)[number];

export function AdminHome() {
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>("staff");
  return (
    <div className="flex flex-col gap-4">
      <nav className="flex gap-1 overflow-x-auto border-b" role="tablist">
        {TABS.map((key) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            className={cn(
              "whitespace-nowrap px-3 py-2 text-sm",
              tab === key ? "border-b-2 border-primary font-semibold text-primary" : "text-muted-foreground",
            )}
            onClick={() => setTab(key)}
          >
            {t(`admin.tabs.${key}`)}
          </button>
        ))}
      </nav>
      {tab === "staff" && <Staff />}
      {tab === "schedules" && <Schedules />}
      {tab === "services" && <Services />}
      {tab === "consent" && <Notices />}
      {tab === "breakGlass" && <BreakGlassQueue />}
      {tab === "reports" && <AccessReports />}
      {tab === "revenue" && <Revenue />}
      {tab === "audit" && <AuditStatus />}
    </div>
  );
}

const STAFF_ROLES: Role[] = ["reception", "nurse", "doctor", "lab_tech", "clinic_admin"];

function Staff() {
  const { t } = useTranslation();
  const client = useQueryClient();
  const members = useQuery({ queryKey: ["members"], queryFn: () => call(() => api.GET("/api/v1/staff/members")) });
  const toggle = useMutation({
    mutationFn: (m: Schemas["StaffMemberOut"]) =>
      call(() =>
        api.PATCH("/api/v1/staff/members/{membership_id}", {
          params: { path: { membership_id: m.membership_id } },
          body: { is_active: !m.is_active },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["members"] }),
  });
  const [form, setForm] = useState({ email: "", name: "", role: "reception" as Role });
  const invite = useMutation({
    mutationFn: () => call(() => api.POST("/api/v1/staff/invites", { body: form })),
    onSuccess: async () => {
      setForm({ email: "", name: "", role: "reception" });
      await client.invalidateQueries({ queryKey: ["members"] });
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    invite.mutate();
  };
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>{t("admin.staff")}</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="divide-y text-sm">
            {members.data?.map((m) => (
              <li key={m.membership_id} className="flex items-center justify-between gap-2 py-2">
                <div>
                  <p className={cn(!m.is_active && "line-through")}>{m.name}</p>
                  <p className="text-xs text-muted-foreground">
                    {t(`roles.${m.role}`)} · {m.email}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  {!m.mfa_enabled && <Badge tone="warning">{t("admin.noMfa")}</Badge>}
                  <Button size="sm" variant="outline" onClick={() => toggle.mutate(m)}>
                    {m.is_active ? t("admin.deactivate") : t("admin.activate")}
                  </Button>
                </div>
              </li>
            ))}
          </ul>
          {toggle.isError && <Alert tone="error">{errorMessage(toggle.error)}</Alert>}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>{t("admin.invite")}</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="flex flex-col gap-3">
            <Field label={t("patient.name")} htmlFor="inv-name">
              <Input id="inv-name" required minLength={2} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </Field>
            <Field label={t("auth.email")} htmlFor="inv-email">
              <Input id="inv-email" type="email" required value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
            </Field>
            <Field label={t("admin.role")} htmlFor="inv-role">
              <Select id="inv-role" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
                {STAFF_ROLES.map((r) => (
                  <option key={r} value={r}>
                    {t(`roles.${r}`)}
                  </option>
                ))}
              </Select>
            </Field>
            {invite.isError && <Alert tone="error">{errorMessage(invite.error)}</Alert>}
            {invite.isSuccess && <Alert tone="success">{t("admin.invited")}</Alert>}
            <Button type="submit" disabled={invite.isPending}>
              {t("admin.sendInvite")}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}

function Schedules() {
  const { t } = useTranslation();
  const client = useQueryClient();
  const schedules = useQuery({ queryKey: ["schedules"], queryFn: () => call(() => api.GET("/api/v1/schedules")) });
  const staff = useQuery({ queryKey: ["staff"], queryFn: () => call(() => api.GET("/api/v1/staff")) });
  const doctors = staff.data?.filter((s) => s.role === "doctor") ?? [];
  const names = new Map(staff.data?.map((s) => [s.user_id, s.name]));
  const [form, setForm] = useState({ doctor_user_id: "", weekday: 0, start_time: "09:00", end_time: "13:00", slot_minutes: 15 });
  const add = useMutation({
    mutationFn: () => call(() => api.POST("/api/v1/schedules", { body: form })),
    onSuccess: () => client.invalidateQueries({ queryKey: ["schedules"] }),
  });
  const toggle = useMutation({
    mutationFn: (s: Schemas["ScheduleOut"]) =>
      call(() =>
        api.PATCH("/api/v1/schedules/{schedule_id}", {
          params: { path: { schedule_id: s.id } },
          body: { status: s.status === "active" ? "inactive" : "active" },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["schedules"] }),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    add.mutate();
  };
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>{t("admin.schedules")}</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="divide-y text-sm">
            {schedules.data?.map((s) => (
              <li key={s.id} className="flex items-center justify-between py-1">
                <span className={cn(s.status !== "active" && "text-muted-foreground line-through")}>
                  {t(`weekdays.${s.weekday}`)} {s.start_time.slice(0, 5)}–{s.end_time.slice(0, 5)} · {names.get(s.doctor_user_id)}
                </span>
                <Button size="sm" variant="ghost" onClick={() => toggle.mutate(s)}>
                  {s.status === "active" ? t("admin.pause") : t("admin.activate")}
                </Button>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>{t("admin.addSchedule")}</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="grid grid-cols-2 gap-3">
            <Field label={t("reception.doctor")} htmlFor="sch-doctor" className="col-span-2">
              <Select id="sch-doctor" required value={form.doctor_user_id} onChange={(e) => setForm({ ...form, doctor_user_id: e.target.value })}>
                <option value="">{t("common.choose")}</option>
                {doctors.map((d) => (
                  <option key={d.user_id} value={d.user_id}>
                    {d.name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label={t("admin.weekday")} htmlFor="sch-day">
              <Select id="sch-day" value={form.weekday} onChange={(e) => setForm({ ...form, weekday: Number(e.target.value) })}>
                {[0, 1, 2, 3, 4, 5, 6].map((d) => (
                  <option key={d} value={d}>
                    {t(`weekdays.${d}`)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label={t("admin.slotMinutes")} htmlFor="sch-slot">
              <Input id="sch-slot" type="number" min={5} max={120} value={form.slot_minutes} onChange={(e) => setForm({ ...form, slot_minutes: Number(e.target.value) })} />
            </Field>
            <Field label={t("admin.from")} htmlFor="sch-start">
              <Input id="sch-start" type="time" value={form.start_time} onChange={(e) => setForm({ ...form, start_time: e.target.value })} />
            </Field>
            <Field label={t("admin.to")} htmlFor="sch-end">
              <Input id="sch-end" type="time" value={form.end_time} onChange={(e) => setForm({ ...form, end_time: e.target.value })} />
            </Field>
            {add.isError && <Alert tone="error" className="col-span-2">{errorMessage(add.error)}</Alert>}
            <Button type="submit" className="col-span-2" disabled={add.isPending}>
              {t("admin.add")}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}

function Services() {
  const { t } = useTranslation();
  const client = useQueryClient();
  const services = useQuery({
    queryKey: ["services", "all"],
    queryFn: () => call(() => api.GET("/api/v1/services", { params: { query: { active: false } } })),
  });
  const [name, setName] = useState("");
  const [price, setPrice] = useState("");
  const add = useMutation({
    mutationFn: () =>
      call(() => api.POST("/api/v1/services", { body: { name, price_paise: Math.round(Number(price) * 100) } })),
    onSuccess: async () => {
      setName("");
      setPrice("");
      await client.invalidateQueries({ queryKey: ["services"] });
    },
  });
  const toggle = useMutation({
    mutationFn: (s: Schemas["ServiceOut"]) =>
      call(() =>
        api.PATCH("/api/v1/services/{service_id}", {
          params: { path: { service_id: s.id } },
          body: { is_active: !s.is_active },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["services"] }),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    add.mutate();
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("admin.services")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <ul className="divide-y text-sm">
          {services.data?.map((s) => (
            <li key={s.id} className="flex items-center justify-between py-1">
              <span className={cn(!s.is_active && "text-muted-foreground line-through")}>
                {s.name} · {formatMoney(s.price_paise)}
              </span>
              <Button size="sm" variant="ghost" onClick={() => toggle.mutate(s)}>
                {s.is_active ? t("admin.pause") : t("admin.activate")}
              </Button>
            </li>
          ))}
        </ul>
        <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
          <Field label={t("admin.serviceName")} htmlFor="svc-name" className="flex-1">
            <Input id="svc-name" required minLength={2} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label={t("admin.priceRupees")} htmlFor="svc-price">
            <Input id="svc-price" inputMode="decimal" required className="w-28" value={price} onChange={(e) => setPrice(e.target.value)} />
          </Field>
          <Button type="submit" disabled={add.isPending}>
            {t("admin.add")}
          </Button>
        </form>
        {add.isError && <Alert tone="error">{errorMessage(add.error)}</Alert>}
      </CardContent>
    </Card>
  );
}

function Notices() {
  const { t } = useTranslation();
  const client = useQueryClient();
  const notices = useQuery({ queryKey: ["notices"], queryFn: () => call(() => api.GET("/api/v1/consent-notices")) });
  const [form, setForm] = useState({ text_en: "", text_hi: "", purposes: "treatment, billing, access_audit" });
  const create = useMutation({
    mutationFn: () =>
      call(() =>
        api.POST("/api/v1/consent-notices", {
          body: {
            text_en: form.text_en,
            text_hi: form.text_hi,
            purposes: form.purposes.split(",").map((p) => p.trim()).filter(Boolean),
          },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["notices"] }),
  });
  const publish = useMutation({
    mutationFn: (id: string) =>
      call(() => api.POST("/api/v1/consent-notices/{notice_id}/publish", { params: { path: { notice_id: id } } })),
    onSuccess: () => client.invalidateQueries({ queryKey: ["notices"] }),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate();
  };
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>{t("admin.notices")}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 text-sm">
          {notices.data?.map((n) => (
            <article key={n.id} className="rounded-md border p-2">
              <p className="font-medium">
                v{n.version}{" "}
                {n.published_at ? (
                  <Badge tone="success">{t("admin.publishedOn", { date: formatDate(n.published_at) })}</Badge>
                ) : (
                  <Badge>{t("notes.draft")}</Badge>
                )}
              </p>
              <p className="line-clamp-3">{n.text_en}</p>
              <p className="line-clamp-3" lang="hi">
                {n.text_hi}
              </p>
              {!n.published_at && (
                <Button size="sm" className="mt-2" onClick={() => publish.mutate(n.id)}>
                  {t("admin.publish")}
                </Button>
              )}
            </article>
          ))}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>{t("admin.newNotice")}</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="flex flex-col gap-3">
            <Field label={t("admin.textEn")} htmlFor="n-en">
              <Textarea id="n-en" required minLength={20} value={form.text_en} onChange={(e) => setForm({ ...form, text_en: e.target.value })} />
            </Field>
            <Field label={t("admin.textHi")} htmlFor="n-hi">
              <Textarea id="n-hi" lang="hi" required minLength={20} value={form.text_hi} onChange={(e) => setForm({ ...form, text_hi: e.target.value })} />
            </Field>
            <Field label={t("admin.purposes")} htmlFor="n-purposes">
              <Input id="n-purposes" value={form.purposes} onChange={(e) => setForm({ ...form, purposes: e.target.value })} />
            </Field>
            {create.isError && <Alert tone="error">{errorMessage(create.error)}</Alert>}
            <Button type="submit" disabled={create.isPending}>
              {t("admin.saveDraft")}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}

function BreakGlassQueue() {
  const { t } = useTranslation();
  const client = useQueryClient();
  const queue = useQuery({ queryKey: ["break-glass"], queryFn: () => call(() => api.GET("/api/v1/break-glass")) });
  const review = useMutation({
    mutationFn: ({ id, outcome }: { id: string; outcome: Schemas["ReviewOutcome"] }) =>
      call(() =>
        api.POST("/api/v1/break-glass/{break_glass_id}/review", {
          params: { path: { break_glass_id: id } },
          body: { outcome },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["break-glass"] }),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("admin.breakGlassQueue")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">
        {queue.data?.length === 0 && <p className="text-muted-foreground">{t("admin.nothingToReview")}</p>}
        {queue.data?.map((b) => (
          <article key={b.id} className="rounded-md border p-2">
            <p>
              <strong>{b.user_name}</strong> {b.user_role && `(${t(`roles.${b.user_role}`)})`} · {b.patient.name}
            </p>
            <p className="text-xs text-muted-foreground">
              {formatDateTime(b.at)} · {t(`breakGlass.codes.${b.reason_code}`)}
            </p>
            <p>{b.reason_text}</p>
            {b.outcome ? (
              <Badge>{t(`outcome.${b.outcome}`)}</Badge>
            ) : (
              <div className="mt-2 flex gap-2">
                {(["benign", "misuse", "unsure"] as const).map((o) => (
                  <Button key={o} size="sm" variant={o === "misuse" ? "destructive" : "outline"} onClick={() => review.mutate({ id: b.id, outcome: o })}>
                    {t(`outcome.${o}`)}
                  </Button>
                ))}
              </div>
            )}
          </article>
        ))}
        {review.isError && <Alert tone="error">{errorMessage(review.error)}</Alert>}
      </CardContent>
    </Card>
  );
}

function AccessReports() {
  const { t } = useTranslation();
  const client = useQueryClient();
  const reports = useQuery({ queryKey: ["access-queries"], queryFn: () => call(() => api.GET("/api/v1/access-queries")) });
  const close = useMutation({
    mutationFn: (id: string) =>
      call(() =>
        api.PATCH("/api/v1/access-queries/{query_id}", { params: { path: { query_id: id } }, body: { status: "closed" } }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["access-queries"] }),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("admin.reports")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">
        {reports.data?.length === 0 && <p className="text-muted-foreground">{t("admin.nothingToReview")}</p>}
        {reports.data?.map((q) => (
          <article key={q.id} className="flex items-start justify-between gap-2 rounded-md border p-2">
            <div>
              <p>{q.message}</p>
              <p className="text-xs text-muted-foreground">{formatDateTime(q.created_at)}</p>
            </div>
            <Button size="sm" variant="outline" onClick={() => close.mutate(q.id)}>
              {t("admin.close")}
            </Button>
          </article>
        ))}
      </CardContent>
    </Card>
  );
}

function Revenue() {
  const { t } = useTranslation();
  const [day, setDay] = useState(() => new Date().toISOString().slice(0, 10));
  const report = useQuery({
    queryKey: ["revenue", day],
    queryFn: () => call(() => api.GET("/api/v1/reports/daily-revenue", { params: { query: { day } } })),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("admin.revenue")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Field label={t("admin.day")} htmlFor="rev-day">
          <Input id="rev-day" type="date" value={day} onChange={(e) => setDay(e.target.value)} />
        </Field>
        {report.data && (
          <dl className="grid grid-cols-2 gap-2 text-sm">
            <dt>{t("admin.totalCollected")}</dt>
            <dd className="font-semibold">{formatMoney(report.data.total_paise)}</dd>
            {Object.entries(report.data.by_method).map(([method, amount]) => (
              <div key={method} className="contents">
                <dt className="text-muted-foreground">{t(`paymentMethod.${method}`)}</dt>
                <dd>{formatMoney(amount)}</dd>
              </div>
            ))}
            <dt className="text-muted-foreground">{t("admin.invoicesPaid")}</dt>
            <dd>{report.data.invoices_paid}</dd>
          </dl>
        )}
      </CardContent>
    </Card>
  );
}

function Check({ ok, label }: { ok: boolean | null | undefined; label: string }) {
  const { t } = useTranslation();
  const tone = ok == null ? "default" : ok ? "success" : "danger";
  const word = ok == null ? t("verify.skipped") : ok ? t("verify.pass") : t("verify.fail");
  return (
    <div className="contents">
      <dt>{label}</dt>
      <dd>
        <Badge tone={tone}>{word}</Badge>
      </dd>
    </div>
  );
}

function AuditStatus() {
  const { t } = useTranslation();
  const status = useQuery({ queryKey: ["audit-status"], queryFn: () => call(() => api.GET("/api/v1/audit/status")) });
  const data = status.data;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("admin.audit.title")}</CardTitle>
        <p className="text-sm text-muted-foreground">{t("admin.audit.explain")}</p>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        {status.isError && <Alert tone="error">{errorMessage(status.error)}</Alert>}
        {data && (
          <>
            {data.problem && <Alert tone="error">{data.problem}</Alert>}
            <dl className="grid grid-cols-2 gap-2">
              <dt>{t("admin.audit.events")}</dt>
              <dd>{data.events}</dd>
              <dt>{t("admin.audit.checkpoint")}</dt>
              <dd>
                {data.sth
                  ? t("admin.audit.checkpointOf", {
                      size: data.sth.tree_size,
                      at: formatDateTime(new Date(Number(data.sth.timestamp)).toISOString()),
                    })
                  : t("admin.audit.none")}
              </dd>
              <Check ok={data.chain_ok} label={t("admin.audit.chain")} />
              <Check ok={data.consistency_ok} label={t("admin.audit.consistency")} />
            </dl>
            {data.sth && (
              <p className="break-all font-mono text-xs text-muted-foreground">
                {t("admin.audit.root")}: {String(data.sth.root_hex)}
              </p>
            )}
            <ul className="flex flex-col gap-1">
              {data.anchors.length === 0 && <li className="text-muted-foreground">{t("admin.audit.noAnchors")}</li>}
              {data.anchors.map((a) => (
                <li key={a.backend} className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-2">
                  <span>{t(`admin.audit.backend.${a.backend}`)}</span>
                  <span className="flex items-center gap-2">
                    <Badge tone={a.status === "confirmed" ? "success" : a.status === "failed" ? "danger" : "warning"}>
                      {t(`admin.audit.status.${a.status}`)}
                    </Badge>
                    {a.link && (
                      <a className="text-primary underline" href={a.link} target="_blank" rel="noreferrer">
                        {t("admin.audit.open")}
                      </a>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}
      </CardContent>
    </Card>
  );
}
