"""Teste de simulação do UniLeap SEM tela, SEM tkinter e SEM pynput reais.

Usa stubs de tkinter/pynput e sockets TCP reais em localhost. Valida a lógica de
passagem de borda, arrastar, teclas, reconexão e segurança do pareamento.
NÃO valida captura/injeção reais do sistema (isso só roda com pynput de verdade).
Rodar:  python tests/test_sim.py
"""
import enum, os, socket, sys, threading, time, types, unittest

# ---------------------------------------------------------------- stubs
class _W:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return lambda *a, **k: _W()

class _Var:
    def __init__(self, value=None, **k): self.v = value
    def get(self): return self.v
    def set(self, v): self.v = v

class _Tk(_W):
    W, H = 1920, 1080
    def __init__(self, *a, **k): self._clip = None; self.clip_sets = 0
    def clipboard_get(self):
        if self._clip is None: raise tkmod.TclError("vazio")
        return self._clip
    def clipboard_clear(self): self._clip = None
    def clipboard_append(self, t): self._clip = (self._clip or "") + t; self.clip_sets += 1
    def winfo_screenwidth(self): return type(self).W
    def winfo_screenheight(self): return type(self).H
    def after(self, *a, **k): pass

tkmod = types.ModuleType("tkinter")
tkmod.Tk, tkmod.StringVar, tkmod.BooleanVar, tkmod.TclError = _Tk, _Var, _Var, type("TclError", (Exception,), {})
tkmod.ttk = types.SimpleNamespace(**{n: _W for n in
    ("Frame", "Label", "Radiobutton", "LabelFrame", "Entry", "Button", "Combobox", "Style", "Checkbutton")})
tkmod.messagebox = types.SimpleNamespace(showinfo=lambda *a, **k: None, showerror=lambda *a, **k: None)
tkmod.simpledialog = types.SimpleNamespace(askstring=lambda *a, **k: "1234")
sys.modules["tkinter"] = tkmod

class FakeListener:
    active = []
    def __init__(self, **kw): self.kw = kw; self.suppress = kw.get("suppress", False); self.daemon = False
    def start(self): FakeListener.active.append(self)
    def wait(self): pass
    def stop(self):
        if self in FakeListener.active: FakeListener.active.remove(self)

class MListener(FakeListener): pass
class KListener(FakeListener): pass

class MController:
    def __init__(self): self._pos = (0, 0); self.log = []
    @property
    def position(self): return self._pos
    @position.setter
    def position(self, p): self._pos = tuple(p); self.log.append(("move", tuple(p)))
    def press(self, b): self.log.append(("press", b.name))
    def release(self, b): self.log.append(("release", b.name))
    def scroll(self, dx, dy): self.log.append(("scroll", dx, dy))

class KController:
    def __init__(self): self.log = []
    def press(self, k): self.log.append(("press", getattr(k, "name", k)))
    def release(self, k): self.log.append(("release", getattr(k, "name", k)))

class Button(enum.Enum):
    unknown = 0; left = 1; middle = 2; right = 3; x1 = 4; x2 = 5

class KeyCode:
    def __init__(self, char=None): self.char = char
    def __eq__(self, o): return isinstance(o, KeyCode) and o.char == self.char
    def __hash__(self): return hash(self.char)

class Key(enum.Enum):
    esc = 1; f8 = 2; enter = 3; shift = 4; ctrl = 5; alt = 6; tab = 7; left = 8

pyn = types.ModuleType("pynput")
pyn.mouse = types.SimpleNamespace(Listener=MListener, Controller=MController, Button=Button)
pyn.keyboard = types.SimpleNamespace(Listener=KListener, Controller=KController, Key=Key, KeyCode=KeyCode)
sys.modules["pynput"] = pyn

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import main as uni  # noqa: E402

# ---------------------------------------------------------------- helpers
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def wait_for(cond, timeout=4.0, msg="timeout"):
    end = time.time() + timeout
    while time.time() < end:
        if cond(): return
        time.sleep(0.01)
    raise AssertionError(msg)

def make_app(w, h):
    _Tk.W, _Tk.H = w, h
    app = uni.UniLeapApp(_Tk())
    def pump():                                   # faz o papel do mainloop do Tk para chamadas de clipboard
        while True: app.run_ui_calls(); time.sleep(0.005)
    threading.Thread(target=pump, daemon=True).start()
    return app

def statuses(app):
    out = []
    while not app.ui_queue.empty():
        out.append(app.ui_queue.get_nowait())
    return [v for t, v in out if t == "status"]

def listeners(app, kind=None):
    return [l for l in FakeListener.active if kind is None or isinstance(l, kind)]

def mouse_l(app, suppress):
    return [l for l in listeners(app, MListener) if l.suppress == suppress]

# ---------------------------------------------------------------- testes puros
class PureTests(unittest.TestCase):
    B = (0, 0, 1919, 1079)
    def test_edges(self):
        self.assertTrue(uni.at_edge("right", 1919, 5, self.B))
        self.assertFalse(uni.at_edge("right", 1000, 5, self.B))
        self.assertTrue(uni.at_edge("left", 0, 5, self.B))
        self.assertTrue(uni.at_edge("top", 5, 0, self.B))
        self.assertTrue(uni.at_edge("bottom", 5, 1079, self.B))
    def test_fraction_and_point(self):
        self.assertAlmostEqual(uni.edge_fraction("right", 1919, 539.5, self.B), 0.5, 3)
        self.assertEqual(uni.point_on_edge("left", 0.5, (0, 0, 1365, 767), 2), (2, 384))
        self.assertEqual(uni.point_on_edge("right", 0.0, self.B, 25), (1894, 0))
        self.assertEqual(uni.point_on_edge("top", 1.0, self.B, 2), (1919, 2))
        self.assertEqual(uni.point_on_edge("bottom", 0.5, self.B, 2), (960, 1077))
    def test_negative_origin(self):                      # monitor à esquerda do principal (Windows)
        b = (-1920, 0, 1919, 1079)
        self.assertEqual(uni.point_on_edge("left", 0.0, b, 10), (-1910, 0))
        self.assertTrue(uni.at_edge("left", -1919, 3, b))
    def test_keys(self):
        self.assertEqual(uni.key_to_data(KeyCode("a")), {"kind": "char", "value": "a"})
        self.assertEqual(uni.key_to_data(KeyCode("A")), {"kind": "char", "value": "A"})
        self.assertEqual(uni.key_to_data(KeyCode("C"), shortcut=True), {"kind": "char", "value": "c"})
        self.assertEqual(uni.key_to_data(KeyCode("\x03")), {"kind": "char", "value": "c"})   # Ctrl+C no Windows
        self.assertEqual(uni.key_to_data(Key.enter), {"kind": "special", "value": "enter"})
        self.assertIsNone(uni.key_to_data(KeyCode(None)))
        self.assertIs(uni.key_from_data({"kind": "special", "value": "enter"}), Key.enter)
        self.assertIsNone(uni.key_from_data({"kind": "special", "value": "__class__"}))
        self.assertIsNone(uni.key_from_data({"kind": "char", "value": "ab"}))
        self.assertIsNone(uni.key_from_data("lixo"))
    def test_buttons(self):
        self.assertEqual(uni.button_to_name(Button.x1), "back")
        self.assertIs(uni.button_from_name("back"), Button.x1)
        self.assertIsNone(uni.button_from_name("name"))
        self.assertIs(uni.button_from_name("left"), Button.left)
    def test_sender_coalesces(self):
        a, b = socket.socketpair()
        s = uni.Sender(a, lambda: None)
        s.q.put({"type": "key", "n": 0}); time.sleep(0.1)
        for i in range(5): s.send({"type": "mouse_delta", "dx": 1, "dy": 2})
        s.send({"type": "mouse_click", "button": "left", "pressed": True})
        s.send({"type": "mouse_delta", "dx": 3, "dy": 3})
        time.sleep(0.2); s.close(); b.settimeout(1)
        data = b.recv(65536).decode().strip().split("\n")
        import json; msgs = [json.loads(x) for x in data]
        deltas = [m for m in msgs if m["type"] == "mouse_delta"]
        self.assertEqual(sum(m["dx"] for m in deltas), 8); self.assertEqual(sum(m["dy"] for m in deltas), 13)
        order = [m["type"] for m in msgs]
        self.assertEqual(order[0], "key")
        self.assertLess(order.index("mouse_click"), len(order) - 1)       # ordem preservada: clique antes do último delta
        self.assertLess(len(deltas), 7)                                      # houve soma de deltas

# ---------------------------------------------------------------- integração
class Integration(unittest.TestCase):
    side = "Direita"
    frozen = False
    def setUp(self):
        FakeListener.active.clear()
        self.port = free_port(); uni.PORT = self.port
        uni.FROZEN = self.frozen; uni.CLIENT_COOLDOWN = 0.0; uni.EDGE_COOLDOWN = 0.0
        self.host = make_app(1920, 1080); self.client = make_app(1366, 768)
        self.host.pair_code.set("1234"); self.host.side_var.set(self.side)
        self.host.start_host()
        wait_for(lambda: self.host.server is not None, msg="host não subiu")
        self.apps = [self.host, self.client]
        self.connect()
        self.sd = uni.SIDES[self.side]
    def tearDown(self):
        for a in self.apps:
            try: a.close()
            except Exception: pass
        FakeListener.active.clear()
    def connect(self, code="1234", app=None):
        c = app or self.client
        c.host_ip.set("127.0.0.1"); tkmod.simpledialog.askstring = lambda *a, **k: code
        c.connect_client()
        return c
    def enter_remote(self):
        h = self.host; B = h.bounds
        wait_for(lambda: h.connected and self.client.connected, msg="não conectou")
        h.cooldown_until = 0
        # cursor em ponto qualquer da borda correta, meio da tela
        x, y = uni.point_on_edge(self.sd, 0.5, B, 0)
        h.h_move(x, y)
        wait_for(lambda: h.state == "remote" and self.client.remote_mode, msg="não entrou no Cliente")
        if not uni.FROZEN:
            cx, cy = h.center; h.h_move(cx, cy)            # evento da própria recentralização
        return x, y

    def test_enter_places_cursor_proportionally_and_suppresses(self):
        self.enter_remote()
        h, c = self.host, self.client
        self.assertEqual(len(mouse_l(h, True)), 1); self.assertEqual(len(mouse_l(h, False)), 0)
        self.assertTrue(all(l.suppress for l in listeners(h)))
        want = uni.point_on_edge(uni.OPPOSITE[self.sd], 0.5, c.bounds, uni.CLIENT_INSET)
        got = c.mouse_ctl.position
        self.assertLessEqual(abs(got[0] - want[0]) + abs(got[1] - want[1]), 1, (got, want))
        self.assertEqual(h.mouse_ctl.position, h.center)             # Host estacionado no centro

    def feed_host_motion(self, dx, dy):
        h = self.host
        if uni.FROZEN:
            h.h_move(h.center[0] + dx, h.center[1] + dy)             # hook suprimido: centro + delta
        else:
            lx, ly = h.last; h.h_move(lx + dx, ly + dy)

    def test_move_click_drag_scroll(self):
        self.enter_remote(); h, c = self.host, self.client
        before = c.mouse_ctl.position
        dx, dy = {"right": (30, 0), "left": (-30, 0), "top": (0, -30), "bottom": (0, 30)}[self.sd]
        self.feed_host_motion(dx, dy)
        wait_for(lambda: c.mouse_ctl.position != before, msg="delta não chegou")
        self.assertEqual(c.mouse_ctl.position, (before[0] + dx, before[1] + dy))
        h.h_click(0, 0, Button.left, True)
        self.feed_host_motion(dx, dy)
        h.h_click(0, 0, Button.left, False)
        h.h_scroll(0, 0, 0, -2)
        wait_for(lambda: ("scroll", 0, -2) in c.mouse_ctl.log, msg="scroll não chegou")
        log = [e for e in c.mouse_ctl.log if e[0] in ("press", "release")]
        self.assertEqual(log, [("press", "left"), ("release", "left")])
        i = c.mouse_ctl.log.index(("press", "left")); j = c.mouse_ctl.log.index(("release", "left"))
        self.assertTrue(any(e[0] == "move" for e in c.mouse_ctl.log[i:j]), "sem movimento durante o arrasto")

    def push_out(self, c, buttons=False):
        # empurra no Cliente contra a borda de retorno
        d = {"left": (-5000, 0), "right": (5000, 0), "top": (0, -5000), "bottom": (0, 5000)}[c.entry_side]
        self.feed_host_motion(*d) if False else c.apply_event({"type": "mouse_delta", "dx": d[0], "dy": d[1]})

    def test_drag_blocks_return_then_release_returns(self):
        self.enter_remote(); h, c = self.host, self.client
        h.h_click(0, 0, Button.left, True)
        wait_for(lambda: "left" in c.pressed_buttons)
        self.push_out(c)
        time.sleep(0.15)
        self.assertTrue(c.remote_mode and h.state == "remote", "arrastando não pode devolver o controle")
        h.h_click(0, 0, Button.left, False)
        wait_for(lambda: not c.pressed_buttons)
        self.push_out(c)
        wait_for(lambda: h.state == "local" and not c.remote_mode, msg="não voltou ao Host")
        # Host recebe o cursor na MESMA borda por onde saiu, proporcional, para dentro
        want = uni.point_on_edge(self.sd, 0.5, h.bounds, uni.HOST_INSET)
        got = h.mouse_ctl.position
        self.assertLessEqual(abs(got[0] - want[0]) + abs(got[1] - want[1]), 4, (got, want))
        self.assertEqual(len(mouse_l(h, False)), 1); self.assertEqual(len(mouse_l(h, True)), 0)

    def test_keys_and_release_all_on_leave(self):
        self.enter_remote(); h, c = self.host, self.client
        h.h_press(KeyCode("a")); h.h_release(KeyCode("a"))
        h.h_press(Key.ctrl); h.h_press(KeyCode("\x03")); h.h_release(KeyCode("\x03")); h.h_release(Key.ctrl)
        h.h_press(Key.enter); h.h_release(Key.enter)
        h.h_press(Key.shift); h.h_press(KeyCode("A"))
        wait_for(lambda: ("press", "A") in c.key_ctl.log, msg="teclas não chegaram")
        self.assertEqual(c.key_ctl.log[:8], [("press", "a"), ("release", "a"), ("press", "ctrl"), ("press", "c"),
                                              ("release", "c"), ("release", "ctrl"), ("press", "enter"), ("release", "enter")])
        self.assertIn(Key.shift, c.pressed_keys)
        self.push_out(c)                                           # sai com shift e 'A' ainda segurados
        wait_for(lambda: h.state == "local")
        self.assertEqual(c.pressed_keys, set())
        self.assertIn(("release", "shift"), c.key_ctl.log); self.assertIn(("release", "A"), c.key_ctl.log)

    def test_clipboard_host_to_client_and_back(self):
        wait_for(lambda: self.host.connected and self.client.connected)
        h, c = self.host, self.client
        h.root._clip = "texto do host: ação ✓"
        self.enter_remote()
        wait_for(lambda: c.root._clip == "texto do host: ação ✓", msg="texto não chegou no Cliente")
        c.root._clip = "copiado no cliente\ncom 2 linhas"                 # Ctrl+C no Cliente
        self.push_out(c)
        wait_for(lambda: h.state == "local" and h.root._clip == "copiado no cliente\ncom 2 linhas", msg="texto não voltou ao Host")

    def test_clipboard_on_esc_and_no_resend_when_unchanged(self):
        self.enter_remote(); h, c = self.host, self.client
        c.root._clip = "via esc"
        h.h_press(Key.esc)
        wait_for(lambda: h.root._clip == "via esc", msg="texto não voltou com Esc")
        sets = h.root.clip_sets
        h.cooldown_until = 0; h.h_press(Key.f8)                          # entra de novo, nada mudou
        wait_for(lambda: h.state == "remote"); h.h_press(Key.f8)
        wait_for(lambda: h.state == "local"); time.sleep(0.2)
        self.assertEqual(h.root.clip_sets, sets, "não deve reenviar texto que não mudou")

    def test_clipboard_can_be_disabled(self):
        wait_for(lambda: self.host.connected and self.client.connected)
        self.host.root._clip = "segredo"; self.host.clip_var.set(False)
        self.enter_remote(); time.sleep(0.2)
        self.assertIsNone(self.client.root._clip)

    def test_clipboard_ignores_oversize_and_empty(self):
        wait_for(lambda: self.host.connected and self.client.connected)
        self.client.clip_receive("x" * (uni.CLIP_MAX + 1)); self.client.clip_receive(""); self.client.clip_receive(123)
        time.sleep(0.1); self.assertIsNone(self.client.root._clip)

    def test_keys_not_forwarded_locally(self):
        wait_for(lambda: self.host.connected and self.client.connected)
        self.host.h_press(KeyCode("z")); time.sleep(0.1)
        self.assertEqual(self.client.key_ctl.log, [])

    def test_esc_returns_and_restores_cursor(self):
        x, y = self.enter_remote(); h, c = self.host, self.client
        h.h_press(Key.esc)
        wait_for(lambda: h.state == "local" and not c.remote_mode, msg="Esc não devolveu")
        self.assertEqual(h.mouse_ctl.position, (x, y))
        self.assertNotIn(("press", "esc"), c.key_ctl.log)

    def test_f8_toggle(self):
        wait_for(lambda: self.host.connected and self.client.connected)
        h, c = self.host, self.client
        h.h_press(Key.f8)
        wait_for(lambda: h.state == "remote" and c.remote_mode, msg="F8 não entrou")
        h.h_press(Key.f8)
        wait_for(lambda: h.state == "local" and not c.remote_mode, msg="F8 não saiu")

    def test_disconnect_while_remote_frees_host_and_allows_reconnect(self):
        self.enter_remote(); h, c = self.host, self.client
        c.disconnect()
        wait_for(lambda: not h.connected and h.state == "local", msg="Host não percebeu")
        self.assertEqual(listeners(h), [], "listeners (grab) não podem ficar ativos sem Cliente")
        wait_for(lambda: not c.running)
        c2 = make_app(1366, 768); self.apps.append(c2)
        self.connect(app=c2)
        wait_for(lambda: h.connected and c2.connected, msg="não reconectou")
        self.assertEqual(len(mouse_l(h, False)), 1)

    def test_wrong_code_rejected_and_retry(self):
        c2 = make_app(1366, 768); self.apps.append(c2)
        # o cliente do setUp já conectou; derruba e testa código errado
        wait_for(lambda: self.host.connected and self.client.connected)
        self.client.disconnect(); wait_for(lambda: not self.host.connected and not self.client.running)
        self.connect(code="9999")
        wait_for(lambda: not self.client.running, msg="cliente ficou preso")
        self.assertFalse(self.host.connected)
        self.assertTrue(any("incorreto" in s for s in statuses(self.client)))
        self.connect(code="1234")
        wait_for(lambda: self.host.connected and self.client.connected, msg="retry falhou")

    def test_second_client_rejected_busy(self):
        wait_for(lambda: self.host.connected and self.client.connected)
        c2 = make_app(1366, 768); self.apps.append(c2)
        self.connect(app=c2)
        wait_for(lambda: not c2.running)
        self.assertTrue(any("já tem um Cliente" in s for s in statuses(c2)))
        self.assertTrue(self.host.connected and self.client.connected)

    def test_no_switch_while_button_held_on_host(self):
        wait_for(lambda: self.host.connected and self.client.connected)
        h = self.host; h.cooldown_until = 0
        h.h_click(5, 5, Button.left, True)
        x, y = uni.point_on_edge(self.sd, 0.5, h.bounds, 0)
        h.h_move(x, y); time.sleep(0.15)
        self.assertEqual(h.state, "local")
        h.h_click(5, 5, Button.left, False); h.h_move(x, y)
        wait_for(lambda: h.state == "remote")

class IntegrationFrozen(Integration): frozen = True
class IntegrationLeft(Integration): side = "Esquerda"
class IntegrationTop(Integration): side = "Acima"
class IntegrationBottom(Integration): side = "Abaixo"

class SpikeGuard(unittest.TestCase):
    def test_x11_stale_events_dropped_until_warp_seen(self):
        FakeListener.active.clear(); uni.FROZEN = False
        h = make_app(1920, 1080); h.state = "remote"; h.sender = types.SimpleNamespace(send=lambda m: sent.append(m))
        sent = []; h.warp(*h.center); h.last = h.center
        h.h_move(1919, 500); self.assertEqual(sent, [])            # evento velho (antes do warp)
        h.h_move(*h.center); h.h_move(h.center[0] + 7, h.center[1] - 3)
        self.assertEqual(sent, [{"type": "mouse_delta", "dx": 7, "dy": -3}])
        h.h_move(h.center[0] + 7 + 600, 100); self.assertEqual(len(sent), 1)   # salto absurdo descartado
        # deriva longe do centro => recentraliza
        h.h_move(h.center[0] + 7 + 500 - 500 + 0, h.center[1]); h.last = (h.center[0] + 10, h.center[1])
        h.h_move(h.center[0] + 10 + 300, h.center[1]); self.assertEqual(h.mouse_ctl.position, h.center)

if __name__ == "__main__":
    unittest.main(verbosity=2)
