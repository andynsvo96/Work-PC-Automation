"""Native, local-only Paycom credential repair prompt.

The helper runs as a separate desktop process so the Paycom PIN never travels
through the dashboard, HTTP request body, server logs, or command line.
"""

from __future__ import annotations

import os
import sys

from credential_store import CredentialStoreError, read_paycom_repair_source, repair_paycom_credential


def show_repair_dialog():
    if os.name != "nt":
        raise RuntimeError("Paycom credential repair is currently available on Windows only.")

    import tkinter as tk
    from tkinter import ttk

    source = read_paycom_repair_source()
    outcome = {"saved": False}

    root = tk.Tk()
    root.title("Repair Paycom Login")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    frame = ttk.Frame(root, padding=20)
    frame.grid(row=0, column=0, sticky="nsew")
    ttk.Label(frame, text="Repair Paycom Login", font=("Segoe UI", 13, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w"
    )
    ttk.Label(
        frame,
        text="The saved username and password will be reused.\nEnter the missing four-digit Paycom PIN.",
        justify="left",
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 14))
    ttk.Label(frame, text="Saved username:").grid(row=2, column=0, sticky="w", pady=4)
    ttk.Label(frame, text=source.username).grid(row=2, column=1, sticky="w", pady=4)
    ttk.Label(frame, text="Paycom PIN:").grid(row=3, column=0, sticky="w", pady=4)
    pin_value = tk.StringVar()
    pin_entry = ttk.Entry(frame, textvariable=pin_value, show="•", width=18)
    pin_entry.grid(row=3, column=1, sticky="ew", pady=4)
    error_value = tk.StringVar()
    ttk.Label(frame, textvariable=error_value, foreground="#b91c1c", wraplength=330).grid(
        row=4, column=0, columnspan=2, sticky="w", pady=(6, 4)
    )

    button_row = ttk.Frame(frame)
    button_row.grid(row=5, column=0, columnspan=2, sticky="e", pady=(10, 0))

    def cancel():
        root.destroy()

    def save():
        pin = pin_value.get().strip()
        try:
            repair_paycom_credential(pin)
        except CredentialStoreError as exc:
            error_value.set(str(exc))
            pin_entry.focus_set()
            pin_entry.selection_range(0, tk.END)
            return
        outcome["saved"] = True
        root.destroy()

    ttk.Button(button_row, text="Cancel", command=cancel).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(button_row, text="Save and Test", command=save).grid(row=0, column=1)
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.bind("<Escape>", lambda _event: cancel())
    root.bind("<Return>", lambda _event: save())
    root.update_idletasks()
    width = root.winfo_reqwidth()
    height = root.winfo_reqheight()
    x = max(0, (root.winfo_screenwidth() - width) // 2)
    y = max(0, (root.winfo_screenheight() - height) // 3)
    root.geometry(f"{width}x{height}+{x}+{y}")
    pin_entry.focus_set()
    root.after(500, lambda: root.attributes("-topmost", False))
    root.mainloop()
    return bool(outcome["saved"])


def main():
    try:
        saved = show_repair_dialog()
    except Exception as exc:
        # Errors contain validation context only; credential values are never
        # interpolated into these messages.
        print(f"PAYCOM_REPAIR_ERROR:{type(exc).__name__}:{exc}", file=sys.stderr)
        return 1
    if not saved:
        print("PAYCOM_REPAIR_CANCELLED")
        return 2
    print("PAYCOM_REPAIR_SAVED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
