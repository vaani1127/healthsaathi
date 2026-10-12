import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Badge, Field, Input, Select, Textarea } from "@/components/ui/form";
import { Empty, QueryStatus } from "@/components/ui/state";
import { useStaffNames } from "@/features/common/queries";
import { PrescriptionBuilder } from "@/features/doctor/PrescriptionBuilder";
import { api, call, type Schemas } from "@/lib/api/client";
import { errorMessage, formatDate, formatDateTime } from "@/lib/format";
import { useAuth } from "@/stores/auth";

type Chart = Schemas["DoctorChart"];
type Note = Schemas["NoteOut"];
type Rx = Schemas["PrescriptionOut"];

/** Ids that have a newer version, so only the latest version of each item gets actions. */
function superseded(items: { parent_id?: string | null }[]): Set<string> {
  return new Set(items.flatMap((i) => (i.parent_id ? [i.parent_id] : [])));
}

export function ConditionsCard({ chart, onChanged }: { chart: Chart; onChanged: () => void }) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const [onset, setOnset] = useState("");
  const add = useMutation({
    mutationFn: () =>
      call(() =>
        api.POST("/api/v1/patients/{patient_id}/conditions", {
          params: { path: { patient_id: chart.patient.id } },
          body: { text: text.trim(), onset: onset || null },
        }),
      ),
    onSuccess: () => {
      setText("");
      setOnset("");
      onChanged();
    },
  });
  const resolve = useMutation({
    mutationFn: (id: string) =>
      call(() =>
        api.PATCH("/api/v1/conditions/{condition_id}", {
          params: { path: { condition_id: id } },
          body: { status: "resolved" },
        }),
      ),
    onSuccess: onChanged,
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    add.mutate();
  };
  const active = chart.conditions.filter((c) => c.status === "active");
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("conditions.title")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        {active.length === 0 && <Empty>{t("conditions.none")}</Empty>}
        <ul className="flex flex-col gap-1">
          {active.map((c) => (
            <li key={c.id} className="flex items-center justify-between gap-2">
              <span>
                {c.text}
                {c.onset && <span className="text-xs text-muted-foreground"> · {formatDate(c.onset)}</span>}
              </span>
              <Button size="sm" variant="ghost" onClick={() => resolve.mutate(c.id)} disabled={resolve.isPending}>
                {t("conditions.resolve")}
              </Button>
            </li>
          ))}
        </ul>
        <form onSubmit={submit} className="flex flex-col gap-2">
          <Field label={t("conditions.new")} htmlFor="cond-text">
            <Input id="cond-text" required minLength={2} maxLength={200} value={text} onChange={(e) => setText(e.target.value)} />
          </Field>
          <Field label={t("conditions.onset")} htmlFor="cond-onset">
            <Input id="cond-onset" type="date" value={onset} onChange={(e) => setOnset(e.target.value)} />
          </Field>
          {(add.isError || resolve.isError) && <Alert tone="error">{errorMessage(add.error ?? resolve.error)}</Alert>}
          <Button type="submit" size="sm" variant="outline" disabled={add.isPending || text.trim().length < 2}>
            {t("conditions.add")}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

export function ReferralsCard({ patientId }: { patientId: string }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const me = useAuth((s) => s.me);
  const names = useStaffNames();
  const referrals = useQuery({
    queryKey: ["referrals", patientId],
    queryFn: () =>
      call(() => api.GET("/api/v1/patients/{patient_id}/referrals", { params: { path: { patient_id: patientId } } })),
  });
  const update = useMutation({
    mutationFn: ({ id, status }: { id: string; status: "completed" | "cancelled" }) =>
      call(() =>
        api.PATCH("/api/v1/referrals/{referral_id}", { params: { path: { referral_id: id } }, body: { status } }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["referrals", patientId] }),
  });
  const name = (id: string) => names.data?.get(id) ?? t("common.unknownStaff");
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("referral.list")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">
        <QueryStatus query={referrals} />
        {referrals.data?.length === 0 && <Empty>{t("referral.none")}</Empty>}
        {update.isError && <Alert tone="error">{errorMessage(update.error)}</Alert>}
        <ul className="flex flex-col gap-2">
          {referrals.data?.map((r) => {
            const mine = r.from_user_id === me?.user_id || r.to_user_id === me?.user_id;
            return (
              <li key={r.id} className="flex flex-col gap-1 rounded-md border p-2">
                <p>
                  {t("referral.fromTo", { from: name(r.from_user_id), to: name(r.to_user_id) })}{" "}
                  <Badge tone={r.status === "active" ? "warning" : "default"}>{t(`referral.status.${r.status}`)}</Badge>
                </p>
                <p className="text-muted-foreground">{r.reason}</p>
                <p className="text-xs text-muted-foreground">{t("referral.validUntil", { at: formatDate(r.valid_until) })}</p>
                {r.status === "active" && mine && (
                  <div className="flex gap-2">
                    <Button size="sm" variant="outline" disabled={update.isPending} onClick={() => update.mutate({ id: r.id, status: "completed" })}>
                      {t("referral.complete")}
                    </Button>
                    <Button size="sm" variant="ghost" disabled={update.isPending} onClick={() => update.mutate({ id: r.id, status: "cancelled" })}>
                      {t("referral.cancel")}
                    </Button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </CardContent>
    </Card>
  );
}

const CARE_ROLES = new Set(["doctor", "nurse", "lab_tech"]);

export function CareTeamCard({ patientId }: { patientId: string }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const staff = useQuery({ queryKey: ["staff"], staleTime: 5 * 60_000, queryFn: () => call(() => api.GET("/api/v1/staff")) });
  const team = useQuery({
    queryKey: ["care-team", patientId],
    queryFn: () =>
      call(() => api.GET("/api/v1/patients/{patient_id}/care-team", { params: { path: { patient_id: patientId } } })),
  });
  const [member, setMember] = useState("");
  const refresh = () => client.invalidateQueries({ queryKey: ["care-team", patientId] });
  const add = useMutation({
    mutationFn: () => {
      const person = staff.data?.find((s) => `${s.user_id}:${s.role}` === member);
      if (!person) {
        throw new Error("no member");
      }
      return call(() =>
        api.POST("/api/v1/care-team", { body: { patient_id: patientId, user_id: person.user_id, role: person.role } }),
      );
    },
    onSuccess: async () => {
      setMember("");
      await refresh();
    },
  });
  const end = useMutation({
    mutationFn: (id: string) =>
      call(() => api.POST("/api/v1/care-team/{assignment_id}/end", { params: { path: { assignment_id: id } } })),
    onSuccess: refresh,
  });
  const names = new Map((staff.data ?? []).map((s) => [s.user_id, s.name]));
  const current = (team.data ?? []).filter((a) => a.status === "active");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    add.mutate();
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("careTeam.title")}</CardTitle>
        <p className="text-sm text-muted-foreground">{t("careTeam.explain")}</p>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        <QueryStatus query={team} />
        {team.data && current.length === 0 && <Empty>{t("careTeam.none")}</Empty>}
        <ul className="flex flex-col gap-1">
          {current.map((a) => (
            <li key={a.id} className="flex items-center justify-between gap-2">
              <span>
                {names.get(a.user_id) ?? t("common.unknownStaff")} · {t(`roles.${a.role}`)}
                <span className="text-xs text-muted-foreground"> · {t("careTeam.since", { at: formatDate(a.starts_at) })}</span>
              </span>
              <Button size="sm" variant="ghost" onClick={() => end.mutate(a.id)} disabled={end.isPending}>
                {t("careTeam.end")}
              </Button>
            </li>
          ))}
        </ul>
        <form onSubmit={submit} className="flex flex-col gap-2 sm:flex-row sm:items-end">
          <Field label={t("careTeam.add")} htmlFor="ct-member" className="flex-1">
            <Select id="ct-member" required value={member} onChange={(e) => setMember(e.target.value)}>
              <option value="">{t("common.choose")}</option>
              {staff.data
                ?.filter((s) => CARE_ROLES.has(s.role))
                .map((s) => (
                  <option key={`${s.user_id}:${s.role}`} value={`${s.user_id}:${s.role}`}>
                    {s.name} · {t(`roles.${s.role}`)}
                  </option>
                ))}
            </Select>
          </Field>
          <Button type="submit" size="sm" variant="outline" disabled={!member || add.isPending}>
            {t("careTeam.addButton")}
          </Button>
        </form>
        {(add.isError || end.isError) && <Alert tone="error">{errorMessage(add.error ?? end.error)}</Alert>}
      </CardContent>
    </Card>
  );
}

function NoteVersions({ noteId }: { noteId: string }) {
  const { t } = useTranslation();
  const history = useQuery({
    queryKey: ["note-history", noteId],
    queryFn: () => call(() => api.GET("/api/v1/notes/{note_id}/history", { params: { path: { note_id: noteId } } })),
  });
  return (
    <div className="flex flex-col gap-2 border-l-2 pl-3">
      <QueryStatus query={history} />
      {history.data?.map((v) => (
        <div key={v.id}>
          <p className="text-xs text-muted-foreground">
            v{v.version} · {formatDateTime(v.created_at)}
            {v.signed_at && ` · ${t("notes.signedAt", { at: formatDateTime(v.signed_at) })}`}
          </p>
          <p className="whitespace-pre-wrap">{v.body}</p>
        </div>
      ))}
    </div>
  );
}

function NoteRow({ note, latest, onChanged }: { note: Note; latest: boolean; onChanged: () => void }) {
  const { t } = useTranslation();
  const me = useAuth((s) => s.me);
  const [amending, setAmending] = useState(false);
  const [showVersions, setShowVersions] = useState(false);
  const [body, setBody] = useState(note.body);
  const amend = useMutation({
    mutationFn: () =>
      call(() => api.POST("/api/v1/notes/{note_id}/versions", { params: { path: { note_id: note.id } }, body: { body } })),
    onSuccess: () => {
      setAmending(false);
      onChanged();
    },
  });
  const sign = useMutation({
    mutationFn: () => call(() => api.POST("/api/v1/notes/{note_id}/sign", { params: { path: { note_id: note.id } } })),
    onSuccess: onChanged,
  });
  const mineDraft = !note.signed_at && note.author_user_id === me?.user_id;
  return (
    <article className="flex flex-col gap-2 rounded-md border p-2">
      <p className="text-xs text-muted-foreground">
        {formatDateTime(note.created_at)} · v{note.version}{" "}
        {note.signed_at ? <Badge tone="success">{t("notes.signedBadge")}</Badge> : <Badge>{t("notes.draft")}</Badge>}
        {!latest && <Badge className="ml-1">{t("notes.superseded")}</Badge>}
      </p>
      {amending ? (
        <Field label={t("notes.amendBody")} htmlFor={`amend-${note.id}`}>
          <Textarea id={`amend-${note.id}`} rows={5} value={body} onChange={(e) => setBody(e.target.value)} />
        </Field>
      ) : (
        <p className="whitespace-pre-wrap">{note.body}</p>
      )}
      {(amend.isError || sign.isError) && <Alert tone="error">{errorMessage(amend.error ?? sign.error)}</Alert>}
      <div className="flex flex-wrap gap-2">
        {mineDraft && (
          <Button size="sm" onClick={() => sign.mutate()} disabled={sign.isPending}>
            {t("notes.sign")}
          </Button>
        )}
        {latest && note.signed_at && !amending && (
          <Button size="sm" variant="outline" onClick={() => setAmending(true)}>
            {t("notes.amend")}
          </Button>
        )}
        {amending && (
          <>
            <Button size="sm" onClick={() => amend.mutate()} disabled={amend.isPending || !body.trim() || body === note.body}>
              {t("notes.saveVersion")}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setAmending(false)}>
              {t("common.cancel")}
            </Button>
          </>
        )}
        {note.version > 1 && (
          <Button size="sm" variant="ghost" aria-expanded={showVersions} onClick={() => setShowVersions((v) => !v)}>
            {showVersions ? t("notes.hideVersions") : t("notes.versions")}
          </Button>
        )}
      </div>
      {showVersions && <NoteVersions noteId={note.id} />}
    </article>
  );
}

function RxRow({ rx, patientId, latest, onChanged }: { rx: Rx; patientId: string; latest: boolean; onChanged: () => void }) {
  const { t } = useTranslation();
  const [amending, setAmending] = useState(false);
  const sign = useMutation({
    mutationFn: () => call(() => api.POST("/api/v1/prescriptions/{rx_id}/sign", { params: { path: { rx_id: rx.id } } })),
    onSuccess: onChanged,
  });
  if (amending) {
    return (
      <PrescriptionBuilder
        patientId={patientId}
        amend={rx}
        onCancel={() => setAmending(false)}
        onSaved={() => {
          setAmending(false);
          onChanged();
        }}
      />
    );
  }
  return (
    <article className="flex flex-col gap-1 rounded-md border p-2">
      <p className="text-xs text-muted-foreground">
        {formatDateTime(rx.created_at)} · v{rx.version}{" "}
        {rx.signed_at ? <Badge tone="success">{t("notes.signedBadge")}</Badge> : <Badge>{t("notes.draft")}</Badge>}
        {!latest && <Badge className="ml-1">{t("notes.superseded")}</Badge>}
      </p>
      <ul>
        {rx.items.map((item, i) => (
          <li key={i}>
            {String(item.drug)} {String(item.strength ?? "")} · {String(item.dose)}
          </li>
        ))}
      </ul>
      {sign.isError && <Alert tone="error">{errorMessage(sign.error)}</Alert>}
      <div className="flex flex-wrap gap-2">
        {rx.signed_at ? (
          <Link
            to="/print/prescription/$rxId"
            params={{ rxId: rx.id }}
            search={{ patient: patientId }}
            className="inline-flex h-8 items-center text-sm text-primary underline"
          >
            {t("rx.print")}
          </Link>
        ) : (
          <Button size="sm" onClick={() => sign.mutate()} disabled={sign.isPending}>
            {t("rx.sign")}
          </Button>
        )}
        {latest && rx.signed_at && (
          <Button size="sm" variant="outline" onClick={() => setAmending(true)}>
            {t("rx.amend")}
          </Button>
        )}
      </div>
    </article>
  );
}

export function NotesHistory({ chart, onChanged }: { chart: Chart; onChanged: () => void }) {
  const { t } = useTranslation();
  const old = superseded(chart.notes);
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("notes.history")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        {chart.notes.length === 0 && <Empty>{t("notes.none")}</Empty>}
        {chart.notes.map((n) => (
          <NoteRow key={n.id} note={n} latest={!old.has(n.id)} onChanged={onChanged} />
        ))}
      </CardContent>
    </Card>
  );
}

export function PrescriptionsHistory({ chart, onChanged }: { chart: Chart; onChanged: () => void }) {
  const { t } = useTranslation();
  const old = superseded(chart.prescriptions);
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("rx.history")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        {chart.prescriptions.length === 0 && <Empty>{t("rx.none")}</Empty>}
        {chart.prescriptions.map((rx) => (
          <RxRow key={rx.id} rx={rx} patientId={chart.patient.id} latest={!old.has(rx.id)} onChanged={onChanged} />
        ))}
      </CardContent>
    </Card>
  );
}
