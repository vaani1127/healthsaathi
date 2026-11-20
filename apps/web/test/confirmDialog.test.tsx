import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import i18n from "@/i18n";

function Harness({ onConfirm }: { onConfirm: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Misuse</button>
      <ConfirmDialog
        open={open}
        title={i18n.t("confirm.reviewTitle")}
        confirmLabel={i18n.t("confirm.recordReview")}
        onCancel={() => setOpen(false)}
        onConfirm={() => {
          onConfirm();
          setOpen(false);
        }}
      >
        <p>{i18n.t("confirm.permanent")}</p>
      </ConfirmDialog>
    </>
  );
}

afterEach(async () => {
  await i18n.changeLanguage("en");
});

describe("ConfirmDialog", () => {
  it("asks first, starts on Cancel, keeps Tab inside and confirms only on the confirm button", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<Harness onConfirm={onConfirm} />);
    await user.click(screen.getByRole("button", { name: "Misuse" }));
    const dialog = screen.getByRole("dialog", { name: "Record this review?" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAccessibleDescription(/cannot be changed later/);
    expect(onConfirm).not.toHaveBeenCalled();
    const cancel = screen.getByRole("button", { name: "Cancel" });
    const confirm = screen.getByRole("button", { name: "Record review" });
    expect(cancel).toHaveFocus();
    await user.tab();
    expect(confirm).toHaveFocus();
    await user.tab();
    expect(cancel).toHaveFocus();
    await user.tab({ shift: true });
    expect(confirm).toHaveFocus();
    await user.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Escape cancels and gives focus back to the button that opened it", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<Harness onConfirm={onConfirm} />);
    const opener = screen.getByRole("button", { name: "Misuse" });
    await user.click(opener);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onConfirm).not.toHaveBeenCalled();
    expect(opener).toHaveFocus();
  });

  it("is translated", async () => {
    await i18n.changeLanguage("hi");
    const user = userEvent.setup();
    render(<Harness onConfirm={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Misuse" }));
    expect(screen.getByRole("dialog", { name: "यह समीक्षा दर्ज करें?" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "रद्द करें" })).toBeInTheDocument();
    expect(screen.getByText(/बदला नहीं जा सकता/)).toBeInTheDocument();
  });
});
