import {
  createRootRoute,
  createRoute,
  createRouter,
  lazyRouteComponent,
  Outlet,
  redirect,
} from "@tanstack/react-router";

import { AppShell } from "@/components/AppShell";
import { UpdatePrompt } from "@/components/UpdatePrompt";
import { LoginPage } from "@/features/auth/LoginPage";
import { PatientLoginPage } from "@/features/auth/PatientLoginPage";
import { SelectClinicPage } from "@/features/auth/SelectClinicPage";
import { TotpPage } from "@/features/auth/TotpPage";
import { HomePage } from "@/features/home/HomePage";
import { NurseQueue } from "@/features/nurse/NurseQueue";
import { ReceptionHome } from "@/features/reception/ReceptionHome";
import { ReceptionPatient } from "@/features/reception/ReceptionPatient";
import type { Role } from "@/lib/api/client";
import { homeFor, useAuth } from "@/stores/auth";

const rootRoute = createRootRoute({
  component: () => (
    <>
      <Outlet />
      <UpdatePrompt />
    </>
  ),
});

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: () => {
    const token = useAuth.getState().token;
    throw redirect({ to: token ? homeFor(token.role) : "/login" });
  },
});

const loginRoute = createRoute({ getParentRoute: () => rootRoute, path: "/login", component: LoginPage });
const totpRoute = createRoute({ getParentRoute: () => rootRoute, path: "/login/totp", component: TotpPage });
const enrolRoute = createRoute({ getParentRoute: () => rootRoute, path: "/login/enrol", component: lazyRouteComponent(() => import("@/features/auth/EnrolPage"), "EnrolPage") });
const selectClinicRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/select-clinic",
  component: SelectClinicPage,
});
const statusRoute = createRoute({ getParentRoute: () => rootRoute, path: "/status", component: HomePage });
const verifyRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/verify",
  component: lazyRouteComponent(() => import("@/features/verify/VerifyPage"), "VerifyPage"),
});
const patientLoginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/patient/login",
  component: PatientLoginPage,
});

const appRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "app",
  component: AppShell,
  beforeLoad: () => {
    const token = useAuth.getState().token;
    if (!token) {
      throw redirect({ to: "/login" });
    }
    if (!token.clinic_id) {
      throw redirect({ to: "/select-clinic" });
    }
  },
});

function only(...roles: Role[]) {
  return () => {
    const role = useAuth.getState().token?.role;
    if (!role || !roles.includes(role)) {
      throw redirect({ to: homeFor(role) });
    }
  };
}

interface VisitSearch {
  appointment?: string;
  token?: string;
}

function visitSearch(search: Record<string, unknown>): VisitSearch {
  const out: VisitSearch = {};
  if (typeof search.appointment === "string") {
    out.appointment = search.appointment;
  }
  if (typeof search.token === "string") {
    out.token = search.token;
  }
  return out;
}

const receptionRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/reception",
  beforeLoad: only("reception"),
  component: ReceptionHome,
});
const receptionPatientRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/reception/patients/$patientId",
  beforeLoad: only("reception"),
  component: ReceptionPatient,
});
const nurseRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/nurse",
  beforeLoad: only("nurse"),
  component: NurseQueue,
});
const nursePatientRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/nurse/patients/$patientId",
  beforeLoad: only("nurse"),
  validateSearch: visitSearch,
  component: lazyRouteComponent(() => import("@/features/nurse/NursePatient"), "NursePatient"),
});
const doctorRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/doctor",
  beforeLoad: only("doctor"),
  component: lazyRouteComponent(() => import("@/features/doctor/DoctorQueue"), "DoctorQueue"),
});
const doctorPatientRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/doctor/patients/$patientId",
  beforeLoad: only("doctor"),
  validateSearch: visitSearch,
  component: lazyRouteComponent(() => import("@/features/doctor/DoctorPatient"), "DoctorPatient"),
});
const printRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/print/prescription/$rxId",
  beforeLoad: only("doctor"),
  validateSearch: (search: Record<string, unknown>) => ({ patient: String(search.patient ?? "") }),
  component: lazyRouteComponent(() => import("@/features/print/PrescriptionPrint"), "PrescriptionPrint"),
});
const labRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/lab",
  beforeLoad: only("lab_tech"),
  component: lazyRouteComponent(() => import("@/features/lab/LabWorklist"), "LabWorklist"),
});
const adminRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/admin",
  beforeLoad: only("clinic_admin"),
  component: lazyRouteComponent(() => import("@/features/admin/AdminHome"), "AdminHome"),
});
const patientRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/patient",
  beforeLoad: only("patient"),
  component: lazyRouteComponent(() => import("@/features/patient/PatientPortal"), "PatientPortal"),
});
const patientPrintRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/patient/print",
  beforeLoad: only("patient"),
  component: lazyRouteComponent(() => import("@/features/patient/RecordPrint"), "RecordPrint"),
});
const invoiceRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/reception/invoices/$invoiceId",
  beforeLoad: only("reception"),
  component: lazyRouteComponent(() => import("@/features/billing/InvoicePage"), "InvoicePage"),
});
const receiptRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/print/receipt/$invoiceId",
  beforeLoad: only("reception", "patient"),
  component: lazyRouteComponent(() => import("@/features/print/ReceiptPrint"), "ReceiptPrint"),
});

const routeTree = rootRoute.addChildren([
  indexRoute,
  loginRoute,
  totpRoute,
  enrolRoute,
  selectClinicRoute,
  statusRoute,
  verifyRoute,
  patientLoginRoute,
  appRoute.addChildren([
    receptionRoute,
    receptionPatientRoute,
    nurseRoute,
    nursePatientRoute,
    doctorRoute,
    doctorPatientRoute,
    printRoute,
    labRoute,
    adminRoute,
    patientRoute,
    patientPrintRoute,
    invoiceRoute,
    receiptRoute,
  ]),
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
