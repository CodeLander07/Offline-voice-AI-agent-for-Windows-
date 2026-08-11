import os
import queue
import sys
import threading
import tkinter as tk
import ctypes
from pathlib import Path
from tkinter import ttk

import javis


APP_DIR = Path(__file__).resolve().parent
LOGO_PATH = APP_DIR / "logo.png"
ICON_PATH = APP_DIR / "logo.ico"
OFFLINE_MARKER = "__Codelander__"
WINDOWS_APP_ID = "ProjectJavis.Javis.DeepMidnight.1"


class QueueWriter:
    def __init__(self, output_queue: "queue.Queue[str]", stream_name: str):
        self.output_queue = output_queue
        self.stream_name = stream_name

    def write(self, text: str) -> int:
        if text:
            self.output_queue.put(text)
        return len(text)

    def flush(self) -> None:
        pass


class CodelanderTerminalApp:
    def __init__(self) -> None:
        self._set_windows_app_id()
        self.root = tk.Tk()
        self.root.title("Codelander")
        self.root.geometry("960x620")
        self.root.minsize(720, 460)
        self.root.configure(bg="#050713")
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.output_queue: "queue.Queue[str]" = queue.Queue()
        self.started = False
        self.agent_thread: threading.Thread | None = None
        self.logo_image: tk.PhotoImage | None = None

        self._build_styles()
        self._build_layout()
        self._apply_window_icon()
        self._poll_output()
        self.root.after(60, self._apply_window_icon)
        self.root.after(300, self.start_agent)

    def _build_styles(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Codelander.TButton",
            background="#111827",
            foreground="#dbeafe",
            bordercolor="#263452",
            focusthickness=0,
            padding=(14, 8),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Codelander.TButton",
            background=[("active", "#1f3b65"), ("disabled", "#101421")],
            foreground=[("disabled", "#64748b")],
        )

    def _build_layout(self) -> None:
        shell = tk.Frame(self.root, bg="#050713")
        shell.pack(fill="both", expand=True, padx=22, pady=18)

        header = tk.Frame(shell, bg="#050713")
        header.pack(fill="x", pady=(0, 14))

        if LOGO_PATH.exists():
            self.logo_image = tk.PhotoImage(file=str(LOGO_PATH))
            logo = tk.Label(header, image=self.logo_image, bg="#050713")
            logo.pack(side="left", padx=(0, 14))

        title_block = tk.Frame(header, bg="#050713")
        title_block.pack(side="left", fill="x", expand=True)

        tk.Label(
            title_block,
            text="Codelander",
            bg="#050713",
            fg="#f8fafc",
            font=("Segoe UI", 24, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_block,
            text="Deep Midnight Command Interface",
            bg="#050713",
            fg="#7dd3fc",
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(2, 0))

        self.status_label = tk.Label(
            header,
            text="OFFLINE",
            bg="#101827",
            fg="#94a3b8",
            font=("Consolas", 10, "bold"),
            padx=12,
            pady=6,
        )
        self.status_label.pack(side="right", padx=(14, 0))

        terminal_frame = tk.Frame(
            shell,
            bg="#0a1020",
            highlightbackground="#1e3a5f",
            highlightcolor="#38bdf8",
            highlightthickness=1,
            bd=0,
        )
        terminal_frame.pack(fill="both", expand=True)

        top_bar = tk.Frame(terminal_frame, bg="#0c1426", height=34)
        top_bar.pack(fill="x")
        top_bar.pack_propagate(False)

        for color in ("#ef4444", "#f59e0b", "#22c55e"):
            tk.Label(top_bar, text="", bg=color, width=2, height=1).pack(
                side="left", padx=(10 if color == "#ef4444" else 4, 0), pady=10
            )

        tk.Label(
            top_bar,
            text="codelander://runtime",
            bg="#0c1426",
            fg="#7dd3fc",
            font=("Consolas", 10),
        ).pack(side="left", padx=14)

        self.output = tk.Text(
            terminal_frame,
            bg="#060914",
            fg="#dbeafe",
            insertbackground="#7dd3fc",
            selectbackground="#1d4ed8",
            selectforeground="#f8fafc",
            relief="flat",
            bd=0,
            padx=18,
            pady=16,
            wrap="word",
            font=("Consolas", 11),
            state="disabled",
        )
        self.output.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(terminal_frame, command=self.output.yview)
        scrollbar.pack(side="right", fill="y")
        self.output.configure(yscrollcommand=scrollbar.set)

        controls = tk.Frame(shell, bg="#050713")
        controls.pack(fill="x", pady=(14, 0))

        self.start_button = ttk.Button(
            controls,
            text="Codelander Starting",
            style="Codelander.TButton",
            command=self.start_agent,
        )
        self.start_button.pack(side="left")

        ttk.Button(
            controls,
            text="Close",
            style="Codelander.TButton",
            command=self.close,
        ).pack(side="right")

        tk.Label(
            controls,
            text="Voice input stays active through your microphone. Runtime text appears here.",
            bg="#050713",
            fg="#64748b",
            font=("Segoe UI", 9),
        ).pack(side="left", padx=14)

        self._append("[system] Interface ready. Codelander will start automatically.\n")

    def _apply_window_icon(self) -> None:
        if ICON_PATH.exists():
            try:
                self.root.iconbitmap(default=str(ICON_PATH))
            except tk.TclError:
                pass
        if self.logo_image is not None:
            self.root.iconphoto(True, self.logo_image)

    @staticmethod
    def _set_windows_app_id() -> None:
        if sys.platform != "win32":
            return
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
        except Exception:
            pass

    def _append(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", text)
        self.output.see("end")
        self.output.configure(state="disabled")

    def _poll_output(self) -> None:
        try:
            while True:
                text = self.output_queue.get_nowait()
                if text == OFFLINE_MARKER:
                    self._mark_offline()
                else:
                    self._append(text)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_output)

    def start_agent(self) -> None:
        if self.started:
            return
        self.started = True
        self.start_button.configure(text="Codelander Running", state="disabled")
        self.status_label.configure(text="ONLINE", fg="#86efac", bg="#10251d")

        self.agent_thread = threading.Thread(target=self._run_agent, daemon=True)
        self.agent_thread.start()

    def _run_agent(self) -> None:
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        writer = QueueWriter(self.output_queue, "stdout")
        sys.stdout = writer
        sys.stderr = writer
        try:
            javis.main()
        except Exception as exc:  # noqa: BLE001
            print(f"[system] Codelander stopped with an error: {exc}")
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr
            self.output_queue.put("[system] Runtime ended.\n")
            self.output_queue.put(OFFLINE_MARKER)

    def _mark_offline(self) -> None:
        self.status_label.configure(text="OFFLINE", fg="#94a3b8", bg="#101827")
        self.start_button.configure(text="Restart Codelander", state="normal")
        self.started = False

    def close(self) -> None:
        self.root.destroy()
        os._exit(0)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    CodelanderTerminalApp().run()
