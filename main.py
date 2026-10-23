import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import socket, threading, json, secrets, string, queue, time, sys
from pynput import mouse, keyboard

PORT = 24800
APP_NAME = "UniLeap"
VERSION = "0.3"
PROTO = 2                       # Host e Cliente precisam usar a mesma versão do protocolo
IS_WIN = sys.platform.startswith("win")
FROZEN = IS_WIN                 # Windows: o hook suprime o mouse e o cursor fica parado no centro
EDGE_COOLDOWN = 0.5             # s sem re-trocar de tela logo após voltar ao Host
CLIENT_COOLDOWN = 0.3           # idem, no Cliente
HOST_INSET = 25                 # px para dentro da borda ao devolver o cursor ao Host
CLIENT_INSET = 2                # px para dentro da borda ao entrar no Cliente
MAX_JUMP = 400                  # descarta saltos absurdos de posição (eventos velhos)
CLIP_MAX = 100_000              # máx. de caracteres de texto sincronizados
LINE_MAX = 1 << 20              # tamanho máximo de um pacote

SIDES = {"Direita": "right", "Esquerda": "left", "Acima": "top", "Abaixo": "bottom"}
SIDE_TEXT = {"right": "direita", "left": "esquerda", "top": "superior", "bottom": "inferior"}
OPPOSITE = {"right": "left", "left": "right", "top": "bottom", "bottom": "top"}
BUTTONS = ("left", "right", "middle", "back", "forward")
BUTTON_ALIASES = {"back": ("x1", "button8"), "forward": ("x2", "button9")}
BUTTON_CANON = {"x1": "back", "button8": "back", "x2": "forward", "button9": "forward"}
MOD_NAMES = {"ctrl", "ctrl_l", "ctrl_r", "alt", "alt_l", "alt_r", "alt_gr", "cmd", "cmd_l", "cmd_r"}


def enable_dpi_awareness():
    """Windows: sem isso, pynput e a escala do Windows discordam sobre as coordenadas."""
    if not IS_WIN:
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try: ctypes.windll.user32.SetProcessDPIAware()
        except Exception: pass


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def screen_bounds(root):
    """(x0, y0, x1, y1) inclusivos da área total da(s) tela(s). Chamar só na thread principal."""
    if IS_WIN:
        try:
            import ctypes
            gm = ctypes.windll.user32.GetSystemMetrics
            x, y, w, h = gm(76), gm(77), gm(78), gm(79)      # área virtual (todos os monitores)
            if w > 0 and h > 0:
                return (x, y, x + w - 1, y + h - 1)
        except Exception:
            pass
    return (0, 0, root.winfo_screenwidth() - 1, root.winfo_screenheight() - 1)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def at_edge(side, x, y, b, margin=1):
    x0, y0, x1, y1 = b
    return {"right": x >= x1 - margin, "left": x <= x0 + margin,
            "top": y <= y0 + margin, "bottom": y >= y1 - margin}[side]


def edge_fraction(side, x, y, b):
    """Posição (0..1) ao longo da borda."""
    x0, y0, x1, y1 = b
    if side in ("left", "right"):
        span, v, o = y1 - y0, y, y0
    else:
        span, v, o = x1 - x0, x, x0
    return clamp((v - o) / span, 0.0, 1.0) if span > 0 else 0.5


def point_on_edge(side, frac, b, inset):
    x0, y0, x1, y1 = b
    frac = clamp(float(frac), 0.0, 1.0)
    if side == "right":  return (x1 - inset, round(y0 + frac * (y1 - y0)))
    if side == "left":   return (x0 + inset, round(y0 + frac * (y1 - y0)))
    if side == "top":    return (round(x0 + frac * (x1 - x0)), y0 + inset)
    return (round(x0 + frac * (x1 - x0)), y1 - inset)


def key_to_data(key, shortcut=False):
    ch = getattr(key, "char", None)
    if ch:
        if len(ch) == 1 and ord(ch) < 32:        # Windows reporta Ctrl+letra como caractere de controle
            code = ord(ch)
            return {"kind": "char", "value": chr(code + 96)} if 1 <= code <= 26 else None
        return {"kind": "char", "value": ch.lower() if shortcut else ch}
    name = getattr(key, "name", None)
    return {"kind": "special", "value": name} if name else None


def key_from_data(data):
    if not isinstance(data, dict):
        return None
    v = data.get("value")
    if data.get("kind") == "char":
        return v if isinstance(v, str) and len(v) == 1 else None
    if isinstance(v, str):
        try: return keyboard.Key[v]
        except KeyError: return None
    return None


def button_to_name(button):
    n = getattr(button, "name", "left")
    return BUTTON_CANON.get(n, n)


def button_from_name(name):
    if name not in BUTTONS:
        return None
    for cand in (name,) + BUTTON_ALIASES.get(name, ()):
        b = getattr(mouse.Button, cand, None)
        if b is not None:
            return b
    return None


def tune_socket(sock):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # sem isso o mouse fica "borrachudo"
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)


class Sender:
    """Envia pacotes em thread própria (não trava a captura) e soma movimentos pendentes do mouse."""
    def __init__(self, sock, on_error):
        self.sock, self.on_error, self.q, self.alive = sock, on_error, queue.Queue(), True
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, msg):
        if self.alive:
            self.q.put(msg)

    def close(self):
        self.alive = False
        self.q.put(None)

    def _run(self):
        pending = []
        try:
            while True:
                msg = pending.pop() if pending else self.q.get()
                if msg is None:
                    return
                if msg.get("type") == "mouse_delta":
                    msg = dict(msg)
                    while True:
                        try: nxt = self.q.get_nowait()
                        except queue.Empty: break
                        if nxt is not None and nxt.get("type") == "mouse_delta":
                            msg["dx"] += nxt["dx"]; msg["dy"] += nxt["dy"]
                        else:
                            pending.append(nxt); break
                self.sock.sendall((json.dumps(msg, separators=(",", ":")) + "\n").encode())
        except OSError:
            if self.alive:
                self.alive = False
                self.on_error()


class UniLeapApp:
    def __init__(self, root):
        self.root = root
        self.root.title("UniLeap")
        scale = 1.0
        if IS_WIN:
            try: scale = clamp(self.root.winfo_fpixels("1i") / 96.0, 1.0, 3.0)
            except Exception: pass
        self.root.geometry(f"{int(580 * scale)}x{int(660 * scale)}")
        self.root.minsize(int(520 * scale), int(620 * scale))
        self.mode = tk.StringVar(value="host")
        self.host_ip = tk.StringVar()
        self.pair_code = tk.StringVar(value="".join(secrets.choice(string.digits) for _ in range(6)))
        self.side_var = tk.StringVar(value="Direita")
        self.hint_var = tk.StringVar(value="Host: mova o cursor até a borda direita para entrar no Cliente.")
        self.status = tk.StringVar(value="Escolha Host ou Cliente.")
        self.clip_var = tk.BooleanVar(value=True)
        self.ui_calls = queue.Queue()       # funções que precisam rodar na thread do Tk
        self.clip_last = None
        self.running = False
        self.connected = False
        self.role = None
        self.server = None
        self.conn = None
        self.conn_file = None
        self.sender = None
        self.ui_queue = queue.Queue()
        self.mouse_ctl = mouse.Controller()
        self.key_ctl = keyboard.Controller()
        self.listeners = []
        self.bounds = screen_bounds(root)
        self.center = ((self.bounds[0] + self.bounds[2]) // 2, (self.bounds[1] + self.bounds[3]) // 2)
        # --- Host ---
        self.side = "right"                 # lado onde o Cliente fica em relação ao Host
        self.state = "local"                # local | switching | remote
        self.slock = threading.Lock()       # troca de estado
        self.tlock = threading.RLock()      # transições / desconexão
        self.tq = queue.Queue()
        self.buttons_down = set()
        self.mods_down = set()
        self.cooldown_until = 0.0
        self.return_pos = None
        self.anchor = self.last = self.center
        self.warp_target = None
        self.warp_deadline = 0.0
        # --- Cliente ---
        self.remote_mode = False            # True enquanto o Host controla este PC
        self.entry_side = "left"
        self.cx, self.cy = self.center
        self.pressed_keys = set()
        self.pressed_buttons = set()
        self.build_ui()
        self.refresh_mode()
        self.root.after(50, self.poll_ui)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    # ------------------------------------------------------------------ UI
    def build_ui(self):
        style = ttk.Style()
        try: style.theme_use("clam")
        except tk.TclError: pass
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="UniLeap", font=("TkDefaultFont", 25, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Mouse e teclado entre Windows e Linux").pack(anchor="w", pady=(0, 14))
        row = ttk.Frame(outer); row.pack(fill="x", pady=(0, 8))
        ttk.Radiobutton(row, text="Este PC é o Host", variable=self.mode, value="host",
                        command=self.refresh_mode).pack(side="left")
        ttk.Radiobutton(row, text="Este PC é o Cliente", variable=self.mode, value="client",
                        command=self.refresh_mode).pack(side="left", padx=14)

        self.host_frame = ttk.LabelFrame(outer, text="Host", padding=10)
        ttk.Label(self.host_frame, text="IP deste PC:").grid(row=0, column=0, sticky="w")
        ttk.Label(self.host_frame, text=get_local_ip(), font=("TkDefaultFont", 11, "bold")).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(self.host_frame, text="Código de pareamento:").grid(row=1, column=0, sticky="w", pady=(8,0))
        ttk.Entry(self.host_frame, textvariable=self.pair_code, width=14, show="*").grid(row=1, column=1, sticky="w", padx=8, pady=(8,0))
        ttk.Label(self.host_frame, text="Cliente fica à:").grid(row=2, column=0, sticky="w", pady=(8,0))
        ttk.Combobox(self.host_frame, textvariable=self.side_var, values=list(SIDES), state="readonly",
                     width=12).grid(row=2, column=1, sticky="w", padx=8, pady=(8,0))
        ttk.Button(self.host_frame, text="Iniciar Host", command=self.start_host).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10,0))

        self.client_frame = ttk.LabelFrame(outer, text="Cliente", padding=10)
        ttk.Label(self.client_frame, text="IP do Host:").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.client_frame, textvariable=self.host_ip, width=25).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(self.client_frame, text="Conectar", command=self.connect_client).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10,0))
        self.client_frame.columnconfigure(1, weight=1)

        ctl = ttk.LabelFrame(outer, text="Controles", padding=10); ctl.pack(fill="x", pady=8)
        self.mode_label = ttk.Label(ctl, text="Estado: desconectado"); self.mode_label.pack(anchor="w")
        ttk.Label(ctl, textvariable=self.hint_var).pack(anchor="w", pady=(5,0))
        ttk.Label(ctl, text="F8 alterna o controle remoto manualmente; Esc devolve o controle ao Host.").pack(anchor="w")
        ttk.Label(ctl, text="Cliente: empurre o cursor contra a borda de volta para devolver o controle.").pack(anchor="w")
        ttk.Checkbutton(ctl, text="Sincronizar texto copiado (Ctrl+C / Ctrl+V) entre os PCs",
                        variable=self.clip_var).pack(anchor="w", pady=(6, 0))
        ttk.Label(outer, text="Use apenas na rede local confiável. Não exponha a porta 24800 à internet.",
                  wraplength=520).pack(anchor="w", pady=(8,0))
        ttk.Label(outer, text=f"Protótipo v{VERSION} • Wayland pode limitar captura/controle",
                  font=("TkDefaultFont", 9)).pack(anchor="w", pady=(6,0))
        self.ctl_frame = ctl

    def refresh_mode(self):
        self.host_frame.pack_forget(); self.client_frame.pack_forget()
        if self.mode.get() == "host":
            self.host_frame.pack(fill="x", pady=5, before=self.ctl_frame)
        else:
            self.client_frame.pack(fill="x", pady=5, before=self.ctl_frame)

    def ui_status(self, msg):
        self.ui_queue.put(("status", msg))

    def run_ui_calls(self):
        while True:
            try: fn, box, ev = self.ui_calls.get_nowait()
            except queue.Empty: return
            try: box.append(fn())
            except Exception: box.append(None)
            if ev: ev.set()

    def run_on_ui(self, fn, wait=True, timeout=1.0):
        """Executa fn na thread do Tk (Tk não é thread-safe). Chamar só de outras threads."""
        box, ev = [], threading.Event() if wait else None
        self.ui_calls.put((fn, box, ev))
        if wait and ev.wait(timeout) and box:
            return box[0]
        return None

    # --- área de transferência (só texto) ---
    def clip_push(self):
        """Envia o texto copiado ao outro PC, se mudou desde a última sincronização."""
        if not self.clip_var.get():
            return
        def get():
            try: return self.root.clipboard_get()
            except tk.TclError: return None
        text = self.run_on_ui(get)
        if isinstance(text, str) and text and text != self.clip_last and len(text) <= CLIP_MAX:
            self.clip_last = text
            self.send({"type": "clipboard", "text": text})

    def clip_receive(self, text):
        if not self.clip_var.get() or not isinstance(text, str) or not text or len(text) > CLIP_MAX:
            return
        self.clip_last = text
        def put():
            self.root.clipboard_clear(); self.root.clipboard_append(text)
        self.run_on_ui(put, wait=False)

    def poll_ui(self):
        # Só atualiza textos; o estado real vive nas threads de rede/entrada.
        self.run_ui_calls()
        try:
            while True:
                typ, val = self.ui_queue.get_nowait()
                if typ == "status":
                    self.status.set(val)
                    self.mode_label.config(text="Estado: " + val)
                elif typ == "remote":
                    self.mode_label.config(text="Estado: " + ("controle no Cliente" if val else "controle neste Host"))
        except queue.Empty:
            pass
        self.root.after(50, self.poll_ui)

    # ---------------------------------------------------------------- Host
    def start_host(self):
        if self.running:
            messagebox.showinfo(APP_NAME, "O UniLeap já está rodando.")
            return
        if len(self.pair_code.get().strip()) < 4:
            messagebox.showerror(APP_NAME, "Use código com pelo menos 4 caracteres.")
            return
        self.side = SIDES.get(self.side_var.get(), "right")
        self.hint_var.set(f"Host: mova o cursor até a borda {SIDE_TEXT[self.side]} para entrar no Cliente.")
        self.role = "host"
        self.running = True
        threading.Thread(target=self.host_server, args=(self.pair_code.get().strip(),), daemon=True).start()
        self.ui_status(f"Host iniciado em {get_local_ip()}:{PORT}; aguardando cliente.")

    def host_server(self, code):
        srv = None
        try:
            srv = socket.socket()
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", PORT)); srv.listen(2); srv.settimeout(1)
            self.server = srv
            while self.running:
                try: conn, addr = srv.accept()
                except socket.timeout: continue
                self.handle_incoming(conn, addr, code)
        except OSError as e:
            if self.running:
                self.ui_status(f"Erro no Host: {e}")
        finally:
            self.running = False
            if srv:
                try: srv.close()
                except OSError: pass

    @staticmethod
    def _reject(conn, reason):
        try: conn.sendall((json.dumps({"type": "rejected", "reason": reason}) + "\n").encode())
        except OSError: pass
        try: conn.close()
        except OSError: pass

    def handle_incoming(self, conn, addr, code):
        try:
            conn.settimeout(5)
            f = conn.makefile("r", encoding="utf-8")
            hello = json.loads(f.readline(4096) or "null")
        except Exception:
            try: conn.close()
            except OSError: pass
            return
        if not isinstance(hello, dict) or hello.get("type") != "hello" or not secrets.compare_digest(
                str(hello.get("code", "")).encode(), code.encode()):
            self.ui_status(f"Conexão recusada de {addr[0]}: código incorreto.")
            self._reject(conn, "code")
            time.sleep(1)
            return
        if hello.get("proto") != PROTO:
            self.ui_status(f"Conexão recusada de {addr[0]}: versão diferente do UniLeap.")
            self._reject(conn, "version")
            return
        if self.connected:
            self._reject(conn, "busy")
            return
        conn.settimeout(None)
        tune_socket(conn)
        self.conn, self.conn_file = conn, f
        self.sender = Sender(conn, self.disconnect)
        self.sender.send({"type": "accepted", "bounds": list(self.bounds)})
        with self.slock:
            self.state = "local"
        self.buttons_down.clear(); self.mods_down.clear()
        self.clip_last = None
        self.cooldown_until = time.monotonic() + 0.3
        self.connected = True
        self.tq = queue.Queue()                       # fila nova por sessão (sem comandos velhos)
        threading.Thread(target=self.transition_loop, args=(self.tq,), daemon=True).start()
        self.start_listeners(False)
        threading.Thread(target=self.host_reader, args=(f,), daemon=True).start()
        self.ui_status(f"Conectado a {addr[0]}. Mova o mouse até a borda {SIDE_TEXT[self.side]}.")

    # --- captura de entrada ---
    def start_listeners(self, suppress):
        kw = {"suppress": True} if suppress else {}
        self.listeners = [
            mouse.Listener(on_move=self.h_move, on_click=self.h_click, on_scroll=self.h_scroll, **kw),
            keyboard.Listener(on_press=self.h_press, on_release=self.h_release, **kw)]
        for l in self.listeners:
            l.daemon = True
            l.start()
        for l in self.listeners:             # espera ficar pronto, sem travar se falhar
            w = getattr(l, "wait", None)
            if w:
                t = threading.Thread(target=w, daemon=True); t.start(); t.join(2.0)

    def stop_listeners(self):
        ls, self.listeners = self.listeners, []
        for l in ls:
            try: l.stop()
            except Exception: pass

    def send(self, msg):
        s = self.sender
        if s:
            s.send(msg)

    def request_remote(self, frac, pos):
        with self.slock:
            if self.state != "local" or not self.connected:
                return
            self.state = "switching"
        self.tq.put(("begin", frac, pos))

    def request_local(self):
        with self.slock:
            if self.state != "remote":
                return
            self.state = "switching"
        self.tq.put(("end", None, True))

    def h_move(self, x, y):
        try:
            st = self.state
            if st == "local":
                if (self.connected and not self.buttons_down and time.monotonic() >= self.cooldown_until
                        and at_edge(self.side, x, y, self.bounds)):
                    self.request_remote(edge_fraction(self.side, x, y, self.bounds), (x, y))
            elif st == "remote":
                self.remote_move(x, y)
        except Exception as e:
            self.ui_status(f"Erro ao processar mouse: {e}")

    def remote_move(self, x, y):
        cx, cy = self.center
        if FROZEN:
            # Windows: o hook suprime o movimento; o cursor fica parado no centro e (x, y) = centro + delta.
            dx, dy = x - self.anchor[0], y - self.anchor[1]
            if abs(dx) > MAX_JUMP or abs(dy) > MAX_JUMP:          # evento velho/fora do esperado
                self.mouse_ctl.position = self.anchor
                return
        else:
            # X11: o cursor se move de verdade; manda diferença entre eventos e recentraliza quando se afasta.
            if self.warp_target is not None:
                if (x, y) == self.warp_target or time.monotonic() > self.warp_deadline:
                    self.warp_target = None
                    self.last = (x, y)
                return
            dx, dy = x - self.last[0], y - self.last[1]
            self.last = (x, y)
            if abs(dx) > MAX_JUMP or abs(dy) > MAX_JUMP:
                dx = dy = 0
            w, h = self.bounds[2] - self.bounds[0], self.bounds[3] - self.bounds[1]
            if abs(x - cx) > w // 4 or abs(y - cy) > h // 4:
                self.warp(cx, cy)
        if dx or dy:
            self.send({"type": "mouse_delta", "dx": dx, "dy": dy})

    def warp(self, x, y):
        self.warp_target = (x, y)
        self.warp_deadline = time.monotonic() + 0.15
        self.mouse_ctl.position = (x, y)

    def h_click(self, x, y, button, pressed):
        try:
            name = button_to_name(button)
            (self.buttons_down.add if pressed else self.buttons_down.discard)(name)
            if self.state == "remote":
                self.send({"type": "mouse_click", "button": name, "pressed": bool(pressed)})
        except Exception as e:
            self.ui_status(f"Erro ao processar clique: {e}")

    def h_scroll(self, x, y, dx, dy):
        try:
            if self.state == "remote":
                self.send({"type": "scroll", "dx": int(dx), "dy": int(dy)})
        except Exception as e:
            self.ui_status(f"Erro ao processar rolagem: {e}")

    def h_press(self, key):
        try:
            name = getattr(key, "name", None)
            if name in MOD_NAMES:
                self.mods_down.add(name)
            st = self.state
            if st == "remote":
                if key in (keyboard.Key.esc, keyboard.Key.f8):
                    self.request_local()
                    return
                data = key_to_data(key, shortcut=bool(self.mods_down - {"shift"}))
                if data:
                    self.send({"type": "key", "action": "press", "key": data})
            elif st == "local" and key == keyboard.Key.f8 and self.connected and not self.buttons_down:
                self.request_remote(0.5, None)
        except Exception as e:
            self.ui_status(f"Erro ao processar tecla: {e}")

    def h_release(self, key):
        try:
            self.mods_down.discard(getattr(key, "name", None))
            if self.state == "remote" and key not in (keyboard.Key.esc, keyboard.Key.f8):
                data = key_to_data(key, shortcut=bool(self.mods_down - {"shift"}))
                if data:
                    self.send({"type": "key", "action": "release", "key": data})
        except Exception as e:
            self.ui_status(f"Erro ao processar tecla: {e}")

    # --- transições (thread própria, nunca dentro do callback do listener) ---
    def transition_loop(self, q):
        while True:
            cmd = q.get()
            if cmd is None:
                return
            try:
                with self.tlock:
                    if not self.connected:
                        continue
                    if cmd[0] == "begin":
                        self._begin_remote(cmd[1], cmd[2])
                    else:
                        self._end_remote(cmd[1], cmd[2])
            except Exception as e:
                self.ui_status(f"Erro ao trocar de tela: {e}")
                try:
                    with self.tlock:
                        self.stop_listeners()
                        if self.connected:
                            self.start_listeners(False)
                        self.state = "local"
                except Exception:
                    pass

    def _begin_remote(self, frac, pos):
        self.clip_push()                              # texto copiado no Host já vale para colar no Cliente
        if pos is None:
            pos = self.mouse_ctl.position
        self.return_pos = tuple(pos)
        self.buttons_down.clear()
        self.stop_listeners()
        self.anchor = self.last = self.center
        self.warp_target = None
        self.start_listeners(True)                    # captura exclusiva: Host não recebe mais nada
        self.warp(*self.center)                       # cursor no centro (espaço para mover em qualquer direção)
        self.send({"type": "enter", "side": self.side, "frac": frac})
        self.state = "remote"
        self.ui_queue.put(("remote", True))

    def _end_remote(self, pos, notify):
        self.stop_listeners()
        if notify:
            self.send({"type": "leave"})
        pos = pos or self.return_pos
        if pos:
            self.mouse_ctl.position = tuple(pos)
        self.buttons_down.clear(); self.mods_down.clear()
        self.cooldown_until = time.monotonic() + EDGE_COOLDOWN
        if self.connected:
            self.start_listeners(False)
        self.state = "local"
        self.ui_queue.put(("remote", False))

    def host_reader(self, f):
        try:
            while True:
                line = f.readline(LINE_MAX)
                if not line:
                    break
                try: msg = json.loads(line)
                except ValueError: continue
                if isinstance(msg, dict) and msg.get("type") == "clipboard":
                    self.clip_receive(msg.get("text"))
                elif isinstance(msg, dict) and msg.get("type") == "leave":
                    try: frac = float(msg.get("frac", 0.5))
                    except (TypeError, ValueError): frac = 0.5
                    with self.slock:
                        if self.state != "remote":
                            continue
                        self.state = "switching"
                    self.tq.put(("end", point_on_edge(self.side, frac, self.bounds, HOST_INSET), False))
        except Exception:
            pass
        if self.conn_file is f:
            self.disconnect()

    # ------------------------------------------------------------- Cliente
    def connect_client(self):
        if self.running: return
        ip = self.host_ip.get().strip()
        if not ip:
            messagebox.showerror(APP_NAME, "Informe o IP mostrado no Host."); return
        code = simpledialog.askstring("Pareamento", "Código mostrado no Host:", show="*")
        if not code: return
        self.role = "client"
        self.running = True
        threading.Thread(target=self.client_worker, args=(ip, code), daemon=True).start()
        self.ui_status("Conectando…")

    def client_worker(self, ip, code):
        reasons = {"code": "código incorreto.", "version": "versão diferente do UniLeap no Host.",
                   "busy": "o Host já tem um Cliente conectado."}
        try:
            conn = socket.create_connection((ip, PORT), timeout=5)
            conn.settimeout(10)
            tune_socket(conn)
            f = conn.makefile("r", encoding="utf-8")
            conn.sendall((json.dumps({"type": "hello", "code": code, "proto": PROTO}) + "\n").encode())
            reply = json.loads(f.readline(4096) or "null")
            if not isinstance(reply, dict) or reply.get("type") != "accepted":
                why = reasons.get(reply.get("reason"), "pareamento recusado.") if isinstance(reply, dict) else "pareamento recusado."
                conn.close()
                raise RuntimeError(why[0].upper() + why[1:])
            conn.settimeout(None)
            self.conn, self.conn_file = conn, f
            self.sender = Sender(conn, self.disconnect)
            self.pressed_keys, self.pressed_buttons = set(), set()
            self.clip_last = None
            self.remote_mode = False
            self.connected = True
            self.ui_status("Conectado. Empurre o cursor contra a borda de volta para devolver o controle ao Host.")
            while True:
                line = f.readline(LINE_MAX)
                if not line:
                    break
                try: msg = json.loads(line)
                except ValueError: continue
                if isinstance(msg, dict):
                    self.apply_event(msg)
            if self.connected:
                self.ui_status("Conexão encerrada pelo Host.")
        except Exception as e:
            self.ui_status(f"Falha na conexão: {e}")
        finally:
            self.disconnect()
            self.running = False

    def apply_event(self, msg):
        try:
            t = msg.get("type")
            if t == "clipboard":
                self.clip_receive(msg.get("text"))
            elif t == "enter":
                side = msg.get("side")
                if side not in OPPOSITE:
                    return
                self.entry_side = OPPOSITE[side]
                self.cx, self.cy = point_on_edge(self.entry_side, msg.get("frac", 0.5), self.bounds, CLIENT_INSET)
                self.mouse_ctl.position = (self.cx, self.cy)
                self.cooldown_until = time.monotonic() + CLIENT_COOLDOWN
                self.remote_mode = True
                self.ui_queue.put(("remote", True))
            elif t == "leave":
                self.remote_mode = False
                self.release_all()
                self.clip_push()                      # texto copiado no Cliente volta para o Host
                self.ui_queue.put(("remote", False))
            elif not self.remote_mode:
                return
            elif t == "mouse_delta":
                self.client_move(int(msg.get("dx", 0)), int(msg.get("dy", 0)))
            elif t == "mouse_click":
                name = msg.get("button")
                btn = button_from_name(name)
                if btn is None:
                    return
                if msg.get("pressed"):
                    self.mouse_ctl.press(btn); self.pressed_buttons.add(name)
                else:
                    self.mouse_ctl.release(btn); self.pressed_buttons.discard(name)
            elif t == "scroll":
                self.mouse_ctl.scroll(int(msg.get("dx", 0)), int(msg.get("dy", 0)))
            elif t == "key":
                key = key_from_data(msg.get("key"))
                if key is None:
                    return
                if msg.get("action") == "press":
                    self.key_ctl.press(key); self.pressed_keys.add(key)
                else:
                    self.key_ctl.release(key); self.pressed_keys.discard(key)
        except Exception as e:
            self.ui_status(f"Erro ao aplicar entrada: {e}")

    def client_move(self, dx, dy):
        x0, y0, x1, y1 = self.bounds
        nx, ny = self.cx + dx, self.cy + dy
        cx, cy = clamp(nx, x0, x1), clamp(ny, y0, y1)
        side = self.entry_side
        out = ((side == "left" and nx < x0) or (side == "right" and nx > x1) or
               (side == "top" and ny < y0) or (side == "bottom" and ny > y1))
        # Arrastando (botão pressionado) o cursor fica preso no Cliente; só devolve com botões soltos.
        if out and not self.pressed_buttons and time.monotonic() >= self.cooldown_until:
            frac = edge_fraction(side, cx, cy, self.bounds)
            self.remote_mode = False
            self.release_all()
            self.clip_push()                          # antes do "leave": o Host já terá o texto ao receber o controle
            self.ui_queue.put(("remote", False))
            if self.sender:
                self.sender.send({"type": "leave", "frac": frac})
            return
        self.cx, self.cy = cx, cy
        self.mouse_ctl.position = (cx, cy)

    def release_all(self):
        for k in list(self.pressed_keys):
            try: self.key_ctl.release(k)
            except Exception: pass
        for name in list(self.pressed_buttons):
            b = button_from_name(name)
            if b is not None:
                try: self.mouse_ctl.release(b)
                except Exception: pass
        self.pressed_keys.clear(); self.pressed_buttons.clear()

    # --------------------------------------------------------------- comum
    def disconnect(self):
        with self.tlock:
            was_connected = self.connected
            self.connected = False
            sender, self.sender = self.sender, None
            if sender:
                sender.close()
            conn, self.conn = self.conn, None
            self.conn_file = None
            if conn:
                try: conn.shutdown(socket.SHUT_RDWR)
                except Exception: pass
                try: conn.close()
                except Exception: pass
            if self.role == "client":
                self.remote_mode = False
                self.release_all()
            else:
                self.tq.put(None)
                self.stop_listeners()
                if was_connected and self.state != "local" and self.return_pos:
                    try: self.mouse_ctl.position = tuple(self.return_pos)
                    except Exception: pass
                with self.slock:
                    self.state = "local"
                if was_connected:
                    self.ui_status("Cliente desconectado; aguardando novo cliente.")
            self.ui_queue.put(("remote", False))

    def close(self):
        self.running = False
        self.disconnect()
        self.stop_listeners()
        if self.server:
            try: self.server.close()
            except Exception: pass
        self.root.destroy()


if __name__ == "__main__":
    enable_dpi_awareness()
    root = tk.Tk()
    app = UniLeapApp(root)
    root.mainloop()
