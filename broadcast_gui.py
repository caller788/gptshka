"""Desktop GUI for scheduling Telegram broadcasts."""
from __future__ import annotations

import asyncio
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from broadcast import BroadcastConfig, run_schedule


class BroadcastApp:
    """Tkinter application that wraps the async broadcaster."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Telegram Broadcast Scheduler")
        self.root.geometry("720x640")

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event: threading.Event | None = None

        self._build_layout()
        self._schedule_log_check()

    # Layout helpers -----------------------------------------------------------------
    def _build_layout(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        container = ttk.Frame(self.root, padding=16)
        container.grid(sticky="nsew")
        for idx in range(3):
            container.columnconfigure(idx, weight=1)
        container.rowconfigure(1, weight=1)
        container.rowconfigure(10, weight=1)
        container.rowconfigure(11, weight=1)

        message_label = ttk.Label(container, text="Сообщение для рассылки:")
        message_label.grid(column=0, row=0, sticky="w", columnspan=3, pady=(0, 4))

        self.message_input = ScrolledText(container, height=6, wrap=tk.WORD)
        self.message_input.grid(column=0, row=1, columnspan=3, sticky="nsew", pady=(0, 12))

        folder_label = ttk.Label(container, text="Название папки Telegram:")
        folder_label.grid(column=0, row=2, sticky="w", pady=(0, 4))

        self.folder_var = tk.StringVar()
        folder_entry = ttk.Entry(container, textvariable=self.folder_var)
        folder_entry.grid(column=0, row=3, sticky="ew", pady=(0, 12))

        session_label = ttk.Label(container, text="Имя/путь сессии (опционально):")
        session_label.grid(column=1, row=2, sticky="w", pady=(0, 4))

        self.session_var = tk.StringVar(value="broadcast")
        session_entry = ttk.Entry(container, textvariable=self.session_var)
        session_entry.grid(column=1, row=3, sticky="ew", padx=(12, 12), pady=(0, 12))

        api_id_label = ttk.Label(container, text="API ID (опционально):")
        api_id_label.grid(column=0, row=4, sticky="w", pady=(0, 4))

        self.api_id_var = tk.StringVar()
        api_id_entry = ttk.Entry(container, textvariable=self.api_id_var)
        api_id_entry.grid(column=0, row=5, sticky="ew", pady=(0, 12))

        api_hash_label = ttk.Label(container, text="API Hash (опционально):")
        api_hash_label.grid(column=1, row=4, sticky="w", pady=(0, 4))

        self.api_hash_var = tk.StringVar()
        api_hash_entry = ttk.Entry(container, textvariable=self.api_hash_var)
        api_hash_entry.grid(column=1, row=5, sticky="ew", padx=(12, 12), pady=(0, 12))

        delays_label = ttk.Label(
            container,
            text="Выберите часы отправки (удерживайте Ctrl или Shift для множественного выбора):",
        )
        delays_label.grid(column=2, row=2, sticky="w", pady=(0, 4))

        self.delays_listbox = tk.Listbox(container, selectmode=tk.MULTIPLE, height=10, exportselection=False)
        for hour in range(0, 25):
            self.delays_listbox.insert(tk.END, str(hour))
        self.delays_listbox.selection_set(0)
        self.delays_listbox.grid(column=2, row=3, rowspan=3, sticky="nsew")

        self.dry_run_var = tk.BooleanVar(value=False)
        access_key_label = ttk.Label(container, text="Одноразовый ключ доступа:")
        access_key_label.grid(column=0, row=6, sticky="w", pady=(0, 4))

        self.access_key_var = tk.StringVar()
        access_key_entry = ttk.Entry(container, textvariable=self.access_key_var, show="*")
        access_key_entry.grid(column=0, row=7, sticky="ew", pady=(0, 12))

        keys_file_label = ttk.Label(container, text="Файл одноразовых ключей (если не указан в .env):")
        keys_file_label.grid(column=1, row=6, sticky="w", pady=(0, 4))

        self.keys_file_var = tk.StringVar(value=os.getenv("BROADCAST_KEYS_FILE", ""))
        keys_file_entry = ttk.Entry(container, textvariable=self.keys_file_var)
        keys_file_entry.grid(column=1, row=7, sticky="ew", padx=(12, 12), pady=(0, 12))

        dry_run_check = ttk.Checkbutton(container, text="Тестовый прогон (без отправки сообщений)", variable=self.dry_run_var)
        dry_run_check.grid(column=0, row=8, columnspan=2, sticky="w", pady=(12, 0))

        buttons_frame = ttk.Frame(container)
        buttons_frame.grid(column=2, row=8, sticky="e", pady=(12, 0))

        self.start_button = ttk.Button(buttons_frame, text="Запустить", command=self.start_broadcast)
        self.start_button.grid(column=0, row=0, padx=(0, 8))

        self.stop_button = ttk.Button(buttons_frame, text="Остановить", command=self.stop_broadcast)
        self.stop_button.grid(column=1, row=0)
        self.stop_button.state(["disabled"])

        logs_label = ttk.Label(container, text="Журнал работы:")
        logs_label.grid(column=0, row=9, sticky="w", columnspan=3, pady=(16, 4))

        self.logs_output = ScrolledText(container, height=12, state=tk.DISABLED, wrap=tk.WORD)
        self.logs_output.grid(column=0, row=10, columnspan=3, sticky="nsew")

    # Runtime helpers ----------------------------------------------------------------
    def start_broadcast(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Рассылка", "Рассылка уже выполняется. Подождите завершения.")
            return

        message = self.message_input.get("1.0", tk.END).strip()
        if not message:
            messagebox.showerror("Ошибка", "Введите текст сообщения для рассылки.")
            return

        folder = self.folder_var.get().strip()
        if not folder:
            messagebox.showerror("Ошибка", "Введите название папки Telegram.")
            return

        delays = [int(self.delays_listbox.get(i)) for i in self.delays_listbox.curselection()]
        if not delays:
            delays = [0]

        session_name = self.session_var.get().strip() or "broadcast"
        api_id_value = self.api_id_var.get().strip() or None
        api_hash_value = self.api_hash_var.get().strip() or None

        try:
            api_id = int(api_id_value) if api_id_value else None
        except ValueError:
            messagebox.showerror("Ошибка", "API ID должно быть числом.")
            return

        access_key = self.access_key_var.get().strip()
        if not access_key:
            messagebox.showerror("Ошибка", "Введите одноразовый ключ доступа.")
            return

        keys_file = self.keys_file_var.get().strip() or None

        config = BroadcastConfig(
            message=message,
            folder=folder,
            delays=delays,
            session=session_name,
            api_id=api_id,
            api_hash=api_hash_value,
            dry_run=self.dry_run_var.get(),
            access_key=access_key,
            keys_file=keys_file,
        )

        self._append_log("Стартуем рассылку...")
        self.start_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.stop_event = threading.Event()
        self.access_key_var.set("")

        def worker() -> None:
            try:
                asyncio.run(
                    run_schedule(
                        config,
                        logger=self.log_queue.put,
                        stop_event=self.stop_event,
                    )
                )
                if self.stop_event and self.stop_event.is_set():
                    self.log_queue.put("Рассылка остановлена пользователем.")
                else:
                    self.log_queue.put("Рассылка завершена.")
            except Exception as exc:  # pragma: no cover - UI feedback path
                self.log_queue.put(f"Ошибка: {exc}")
            finally:
                self.root.after(0, self._on_worker_complete)

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _on_worker_complete(self) -> None:
        self.start_button.state(["!disabled"])
        self.stop_button.state(["disabled"])
        self.stop_event = None

    def _append_log(self, message: str) -> None:
        self.logs_output.configure(state=tk.NORMAL)
        self.logs_output.insert(tk.END, message + "\n")
        self.logs_output.see(tk.END)
        self.logs_output.configure(state=tk.DISABLED)

    def _schedule_log_check(self) -> None:
        self._drain_log_queue()
        self.root.after(150, self._schedule_log_check)

    def _drain_log_queue(self) -> None:
        while not self.log_queue.empty():
            message = self.log_queue.get_nowait()
            self._append_log(message)

    def stop_broadcast(self) -> None:
        if self.stop_event and not self.stop_event.is_set():
            self.stop_event.set()
            self._append_log("Запрошена остановка рассылки...")
        elif self.worker and self.worker.is_alive():
            self._append_log("Остановка уже запрошена, ожидаем завершение...")
        else:
            messagebox.showinfo("Рассылка", "Нет активной рассылки для остановки.")


def main() -> None:
    root = tk.Tk()
    BroadcastApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
