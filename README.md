# UniLeap v0.3 — protótipo

Compartilha mouse e teclado entre dois PCs (Windows ↔ Linux) na rede local, no estilo Input Leap.
**Host e Cliente precisam estar na v0.3** (o protocolo mudou e a v0.2 é recusada).

## Instalar
### Artix / Debian (use sessão X11)
```bash
# Artix:  sudo pacman -S python python-pip tk
# Debian: sudo apt install python3-tk python3-venv
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt     # no Linux o pynput já traz o python-xlib
python main.py                      # ou ./run-linux.sh
```
### Windows
Instale Python 3.10+ (marque *Add Python to PATH*), depois:
```powershell
py -m pip install -r requirements.txt
py main.py                          # ou run-windows.bat
```
Libere a porta **24800/TCP** no firewall do PC que será Host (rede privada). No Linux, só se houver firewall ativo.

## Usar
1. No PC com o mouse/teclado: **Este PC é o Host**, escolha em que lado o Cliente fica (padrão: Direita) e clique **Iniciar Host**.
2. No outro PC: **Este PC é o Cliente**, digite o IP do Host, clique **Conectar** e informe o código.
3. Leve o cursor até a borda do Host voltada para o Cliente: ele aparece no Cliente, na altura proporcional.
4. Para voltar: empurre o cursor contra a borda oposta no Cliente, ou use **Esc** no Host. **F8** alterna manualmente.
5. Segurando um botão (arrastando), o cursor não troca de tela; solte o botão para trocar.
6. **Copiar e colar (texto):** copie com Ctrl+C num PC, passe o cursor para o outro e cole com Ctrl+V. O texto copiado é enviado a cada troca de tela, nos dois sentidos. Desmarque "Sincronizar texto copiado" se não quiser isso.

Enquanto o Cliente está ativo, o Host não recebe mais mouse/teclado (captura exclusiva). Se algo travar: **Esc**.

## Limitações
- TCP sem criptografia: só em rede local confiável; nunca exponha a porta 24800 à internet.
- Linux: use X11. Wayland bloqueia captura global/injeção de eventos.
- A área de transferência sincroniza só **texto** (até 100 000 caracteres), e só na troca de tela. Imagens e arquivos copiados não passam. O texto vai sem criptografia: desmarque a sincronização ao copiar senhas.
- Linux: o texto fica disponível enquanto o UniLeap estiver aberto (a área de transferência é do próprio programa).
- Não arrasta janelas (ex.: picture-in-picture) nem arquivos entre PCs, e não suporta mais de um Cliente.
- Teclas: letras, números, símbolos e teclas especiais comuns. Teclas só identificáveis por código (alguns teclados multimídia/numérico em certos casos) são ignoradas. Ctrl+Alt+Del e atalhos tratados pelo sistema não passam.
- Vários monitores: a borda usada é a da área total; monitores de tamanhos diferentes podem ter trechos de borda inalcançáveis.
- Com a captura ativa no Linux, o cursor do Host fica visível, parado perto do centro da tela.
- Teste automático: `python tests/test_sim.py` (simula Host/Cliente com stubs; não usa tela nem pynput real).
