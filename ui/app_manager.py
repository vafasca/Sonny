"""
ui/app_manager.py — UI para gestionar los items de Sonny.
Ejecutar directamente: python ui/app_manager.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import subprocess, threading, time

from core.registry import (
    BUILTIN, SYSTEM_CMDS, EXT_TYPE, item_type, item_exists,
    load_custom, save_custom, get_all
)

# ── Helpers ────────────────────────────────────────────────────────────────────
def clean_name(raw: str) -> str:
    name = os.path.splitext(os.path.basename(raw))[0]
    return name.lower().split("(")[0].split("-")[0].strip().replace(" ", "_")

def test_launch(path: str) -> bool:
    try:
        if path.endswith(".lnk"):
            p = subprocess.Popen(["cmd", "/c", "start", "", path],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif path in SYSTEM_CMDS:
            return True
        else:
            p = subprocess.Popen(path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.2)
        p.poll()
        if path.endswith(".lnk") or p.returncode is None:
            try: p.terminate()
            except: pass
            return True
        return False
    except Exception:
        return False

def scan_folder(folder: str) -> list[str]:
    found = []
    skip  = {"uninstall","update","crash","helper","setup","install","repair","redist"}
    for root, dirs, files in os.walk(folder):
        depth = root.replace(folder, "").count(os.sep)
        if depth >= 2:
            dirs[:] = []
            continue
        for f in files:
            if f.lower().endswith((".exe", ".lnk")):
                if not any(x in f.lower() for x in skip):
                    found.append(os.path.join(root, f))
    found.sort(key=lambda x: (0 if x.endswith(".exe") else 1, x))
    return found

# ── Paleta ─────────────────────────────────────────────────────────────────────
BG=("#0f0f13"); BG2=("#16161e"); BG3=("#1e1e2a"); BORDER=("#2a2a3a")
ACCENT=("#7c6af7"); ACCENT2=("#a78bfa"); GREEN=("#4ade80"); RED=("#f87171")
FG=("#e2e0ff"); FG_DIM=("#6e6a8a"); FONT=("Consolas",10); FONTS=("Consolas",9)

# ── UI ─────────────────────────────────────────────────────────────────────────
class AppManager(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Sonny — Gestor de Items")
        self.geometry("860x580"); self.minsize(700,460)
        self.configure(bg=BG); self.resizable(True,True)
        self.custom = load_custom()
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.refresh())
        self._build(); self.refresh()

    def _build(self):
        # Header
        h = tk.Frame(self, bg=BG, pady=14, padx=24); h.pack(fill="x")
        tk.Label(h, text="⚡ SONNY", font=("Consolas",18,"bold"), fg=ACCENT2, bg=BG).pack(side="left")
        tk.Label(h, text="  gestor de items", font=("Consolas",11), fg=FG_DIM, bg=BG).pack(side="left")

        # Barra
        bar = tk.Frame(self, bg=BG, padx=24, pady=4); bar.pack(fill="x")
        tk.Label(bar, text="🔍", bg=BG, fg=FG_DIM, font=FONT).pack(side="left")
        tk.Entry(bar, textvariable=self.filter_var, bg=BG3, fg=FG, insertbackground=ACCENT2,
                 relief="flat", font=FONT, highlightthickness=1,
                 highlightbackground=BORDER, highlightcolor=ACCENT
                 ).pack(side="left", fill="x", expand=True, ipady=6, padx=(6,12))
        tk.Button(bar, text="＋  Manual", font=FONT, bg=BG3, fg=FG,
                  activebackground=ACCENT, activeforeground="white", relief="flat",
                  cursor="hand2", padx=10, pady=4, command=self.dlg_manual
                  ).pack(side="right", padx=(6,0))
        tk.Button(bar, text="🔍  Auto-detectar", font=FONT, bg=ACCENT, fg="white",
                  activebackground=ACCENT2, activeforeground="white", relief="flat",
                  cursor="hand2", padx=10, pady=4, command=self.dlg_auto
                  ).pack(side="right")

        # Tabla
        tf = tk.Frame(self, bg=BG, padx=24, pady=8); tf.pack(fill="both", expand=True)
        s = ttk.Style(); s.theme_use("clam")
        s.configure("S.Treeview", background=BG2, foreground=FG, rowheight=30,
                    fieldbackground=BG2, borderwidth=0, font=FONT)
        s.configure("S.Treeview.Heading", background=BG3, foreground=ACCENT2,
                    font=("Consolas",9,"bold"), relief="flat")
        s.map("S.Treeview", background=[("selected",ACCENT)], foreground=[("selected","white")])

        self.tree = ttk.Treeview(tf, columns=("st","nombre","tipo","fuente","ruta"),
                                  show="headings", style="S.Treeview", selectmode="browse")
        for col,w,txt in [("st",28,""),("nombre",130,"NOMBRE"),("tipo",70,"TIPO"),
                           ("fuente",95,"FUENTE"),("ruta",400,"RUTA")]:
            self.tree.heading(col, text=txt)
            self.tree.column(col, width=w, anchor="center" if col in ("st","tipo","fuente") else "w",
                              stretch=(col=="ruta"))
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("ok",     foreground=GREEN)
        self.tree.tag_configure("miss",   foreground=RED)
        self.tree.tag_configure("custom", foreground=ACCENT2)

        # Footer
        foot = tk.Frame(self, bg=BG, padx=24, pady=10); foot.pack(fill="x")
        self.lbl = tk.Label(foot, text="", font=FONTS, fg=FG_DIM, bg=BG); self.lbl.pack(side="left")
        tk.Button(foot, text="🗑  Eliminar", font=FONTS, bg=BG3, fg=RED,
                  activebackground="#2a1a1a", activeforeground=RED, relief="flat",
                  cursor="hand2", padx=10, pady=4, command=self.delete
                  ).pack(side="right", padx=(8,0))
        tk.Button(foot, text="▶  Abrir", font=FONTS, bg=BG3, fg=GREEN,
                  activebackground="#0a2a1a", activeforeground=GREEN, relief="flat",
                  cursor="hand2", padx=10, pady=4, command=self.open_sel
                  ).pack(side="right")

    def refresh(self):
        for r in self.tree.get_children(): self.tree.delete(r)
        q = self.filter_var.get().lower()
        all_items = {**{n:{"path":p,"src":"builtin"} for n,p in BUILTIN.items()},
                     **{n:{"path":p,"src":"custom"} for n,p in self.custom.items()}}
        shown = 0
        for name, info in sorted(all_items.items()):
            if q and q not in name.lower() and q not in info["path"].lower(): continue
            ex   = item_exists(info["path"])
            src  = info["src"]
            self.tree.insert("", "end",
                values=("✅" if ex else "❌", name,
                        f"[{item_type(info['path'])}]",
                        "📌 custom" if src=="custom" else "builtin",
                        info["path"]),
                tags=(("custom" if src=="custom" else ("ok" if ex else "miss")),))
            shown += 1
        total = len(all_items)
        det   = sum(1 for i in all_items.values() if item_exists(i["path"]))
        self.lbl.config(text=f"{det}/{total} detectados  |  {len(self.custom)} custom  |  mostrando {shown}")

    # ── Diálogo Manual ─────────────────────────────────────────────────────────
    def dlg_manual(self):
        win = tk.Toplevel(self); win.title("Agregar item"); win.geometry("540x250")
        win.configure(bg=BG); win.resizable(False,False); win.grab_set()
        tk.Label(win, text="Agregar item manualmente", font=("Consolas",12,"bold"),
                 fg=ACCENT2, bg=BG, pady=14).pack()
        form = tk.Frame(win, bg=BG, padx=24); form.pack(fill="x")

        tk.Label(form, text="Nombre (ej: obs, mi_video, contrato):",
                 font=FONTS, fg=FG_DIM, bg=BG, anchor="w"
                 ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0,2))
        name_var = tk.StringVar()
        tk.Entry(form, textvariable=name_var, bg=BG3, fg=FG, insertbackground=ACCENT2,
                 relief="flat", font=FONT, highlightthickness=1,
                 highlightbackground=BORDER, highlightcolor=ACCENT
                 ).grid(row=1, column=0, columnspan=2, sticky="ew", ipady=6, pady=(0,10))

        tk.Label(form, text="Ruta (app, imagen, video, doc...):",
                 font=FONTS, fg=FG_DIM, bg=BG, anchor="w"
                 ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0,2))
        path_var = tk.StringVar()
        pr = tk.Frame(form, bg=BG); pr.grid(row=3, column=0, columnspan=2, sticky="ew")
        form.columnconfigure(0, weight=1)
        tk.Entry(pr, textvariable=path_var, bg=BG3, fg=FG, insertbackground=ACCENT2,
                 relief="flat", font=FONT, highlightthickness=1,
                 highlightbackground=BORDER, highlightcolor=ACCENT
                 ).pack(side="left", fill="x", expand=True, ipady=6)

        def browse():
            f = filedialog.askopenfilename(title="Selecciona el archivo",
                filetypes=[("Todos","*.*"),("Apps","*.exe *.lnk"),
                           ("Imágenes","*.jpg *.jpeg *.png *.gif *.webp"),
                           ("Videos","*.mp4 *.mkv *.avi *.mov"),
                           ("Audio","*.mp3 *.wav *.flac"),
                           ("Documentos","*.pdf *.docx *.xlsx *.txt")])
            if f:
                f = f.replace("/","\\"); path_var.set(f)
                if not name_var.get(): name_var.set(clean_name(f))

        tk.Button(pr, text="📂", font=FONT, bg=ACCENT, fg="white",
                  relief="flat", cursor="hand2", padx=8, command=browse
                  ).pack(side="right", padx=(6,0), ipady=2)

        def save():
            name = name_var.get().strip().lower().replace(" ","_")
            path = path_var.get().strip()
            if not name or not path:
                messagebox.showwarning("Faltan datos","Completa nombre y ruta.",parent=win); return
            self.custom[name] = path; save_custom(self.custom); self.refresh(); win.destroy()

        tk.Button(win, text="✅  Guardar", font=FONT, bg=ACCENT, fg="white",
                  activebackground=ACCENT2, relief="flat", cursor="hand2",
                  padx=16, pady=6, command=save).pack(pady=14)

    # ── Diálogo Auto-detectar ──────────────────────────────────────────────────
    def dlg_auto(self):
        win = tk.Toplevel(self); win.title("Auto-detectar app"); win.geometry("600x420")
        win.configure(bg=BG); win.resizable(False,False); win.grab_set()
        tk.Label(win, text="🔍  Auto-detectar app", font=("Consolas",13,"bold"),
                 fg=ACCENT2, bg=BG, pady=14).pack()
        tk.Label(win, text="Selecciona la carpeta donde está instalada la app.\n"
                 "Sonny buscará ejecutables, los probará y guardará el que funcione.",
                 font=FONTS, fg=FG_DIM, bg=BG, justify="center").pack()

        form = tk.Frame(win, bg=BG, padx=30, pady=10); form.pack(fill="x")
        tk.Label(form, text="Nombre:", font=FONTS, fg=FG_DIM, bg=BG, anchor="w").pack(fill="x",pady=(0,2))
        name_var = tk.StringVar()
        tk.Entry(form, textvariable=name_var, bg=BG3, fg=FG, insertbackground=ACCENT2,
                 relief="flat", font=FONT, highlightthickness=1,
                 highlightbackground=BORDER, highlightcolor=ACCENT).pack(fill="x",ipady=6,pady=(0,10))

        tk.Label(form, text="Carpeta:", font=FONTS, fg=FG_DIM, bg=BG, anchor="w").pack(fill="x",pady=(0,2))
        folder_var = tk.StringVar()
        row = tk.Frame(form, bg=BG); row.pack(fill="x")
        tk.Entry(row, textvariable=folder_var, bg=BG3, fg=FG, insertbackground=ACCENT2,
                 relief="flat", font=FONT, highlightthickness=1,
                 highlightbackground=BORDER, highlightcolor=ACCENT
                 ).pack(side="left", fill="x", expand=True, ipady=6)

        def browse_f():
            d = filedialog.askdirectory(title="Selecciona la carpeta")
            if d:
                folder_var.set(d.replace("/","\\")); 
                if not name_var.get(): name_var.set(clean_name(d))

        tk.Button(row, text="📂", font=FONT, bg=ACCENT, fg="white",
                  relief="flat", cursor="hand2", padx=8, command=browse_f
                  ).pack(side="right", padx=(6,0), ipady=2)

        lf = tk.Frame(win, bg=BG, padx=30); lf.pack(fill="both", expand=True, pady=(10,0))
        log_box = tk.Text(lf, bg=BG2, fg=FG, font=FONTS, relief="flat",
                          height=8, state="disabled")
        log_box.pack(fill="both", expand=True)

        def log(msg):
            log_box.configure(state="normal"); log_box.insert("end", msg+"\n")
            log_box.see("end"); log_box.configure(state="disabled"); win.update_idletasks()

        btn = tk.Button(win, text="⚡  Escanear y probar", font=FONT, bg=ACCENT, fg="white",
                        activebackground=ACCENT2, relief="flat", cursor="hand2", padx=14, pady=6)
        btn.pack(pady=12)

        def run():
            name = name_var.get().strip().lower().replace(" ","_")
            folder = folder_var.get().strip()
            if not name:
                messagebox.showwarning("Falta nombre","Escribe un nombre.",parent=win); return
            if not folder or not os.path.isdir(folder):
                messagebox.showwarning("Carpeta inválida","Selecciona una carpeta válida.",parent=win); return
            btn.configure(state="disabled", text="Escaneando...")
            log_box.configure(state="normal"); log_box.delete("1.0","end"); log_box.configure(state="disabled")

            def worker():
                log(f"📁 Escaneando: {folder}")
                cands = scan_folder(folder)
                if not cands:
                    log("❌ No se encontraron ejecutables."); btn.configure(state="normal",text="⚡  Escanear y probar"); return
                log(f"🔎 {len(cands)} archivos. Probando...\n"); winner=None
                for path in cands:
                    log(f"  ⏳ {os.path.basename(path)}")
                    if test_launch(path):
                        log(f"  ✅ Funciona: {path}\n"); winner=path; break
                    else:
                        log(f"  ❌ No abrió: {os.path.basename(path)}")
                if winner:
                    self.custom[name]=winner; save_custom(self.custom); self.refresh()
                    log(f"💾 Guardado como '{name}'")
                    messagebox.showinfo("✅",f"'{name}' guardado.\n{winner}",parent=win); win.destroy()
                else:
                    log("⚠️  Ningún archivo funcionó. Usa ＋ Manual.")
                btn.configure(state="normal",text="⚡  Escanear y probar")

            threading.Thread(target=worker, daemon=True).start()

        btn.configure(command=run)

    # ── Acciones tabla ─────────────────────────────────────────────────────────
    def delete(self):
        sel = self.tree.selection()
        if not sel: return
        vals = self.tree.item(sel[0],"values")
        name, fuente = vals[1], vals[3]
        if "custom" not in fuente:
            messagebox.showinfo("No editable",f"'{name}' es builtin.",parent=self); return
        if messagebox.askyesno("Eliminar",f"¿Eliminar '{name}'?",parent=self):
            self.custom.pop(name,None); save_custom(self.custom); self.refresh()

    def open_sel(self):
        sel = self.tree.selection()
        if not sel: return
        vals = self.tree.item(sel[0],"values")
        name, path = vals[1], vals[4]
        try:
            if path in SYSTEM_CMDS: subprocess.Popen(path)
            elif path.lower().endswith(".exe"):
                try: subprocess.Popen(path)
                except: os.startfile(path)
            else: os.startfile(path)
            messagebox.showinfo("✅",f"Abriendo {name}...",parent=self)
        except Exception as e:
            messagebox.showerror("Error",str(e),parent=self)

if __name__ == "__main__":
    AppManager().mainloop()
