import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Input } from "@/components/ui/form";

/** A password input with a "Show" button, so people can check what they typed (ASVS 2.1.12). */
export function PasswordInput(props: Omit<React.InputHTMLAttributes<HTMLInputElement>, "type">) {
  const { t } = useTranslation();
  const [visible, setVisible] = useState(false);
  return (
    <div className="relative">
      <Input {...props} type={visible ? "text" : "password"} className="pr-16" />
      <button
        type="button"
        className="absolute inset-y-0 right-0 px-3 text-sm text-primary underline"
        aria-pressed={visible}
        aria-controls={props.id}
        onClick={() => setVisible((v) => !v)}
      >
        {visible ? t("auth.hidePassword") : t("auth.showPassword")}
      </button>
    </div>
  );
}
