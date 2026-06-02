#!/usr/bin/env python3

"""
Bridge APRS-FI -> LoRa MeshCom via net console TCP/Telnet.

richiesto FW 4.35p 23.06.2026 o superiori

Regole principali:
- abilitare la funzione net console nel nodo MeshCom che usiamo
- il JSON deve essere type == "msg";
- il campo dst deve essere uguale al nodo configurato in MY_NODE;
- il testo deve iniziare con APRS-FI:;
- subito dopo deve esserci il gruppo nel formato Gnnn / Gnnnn / Gnnnnn;
- il gruppo deve essere autorizzato da ALLOWED_GROUPS;
- il messaggio viene inviato verso MeshCom come: ::{gruppo} testo
"""

import json
import re
import select
import socket
import sys
import time


# ============================================================
# CONFIGURAZIONE
# ============================================================

TELNET_HOST = "192.168.1.143"
TELNET_PORT = 2323

# Stringa che la net console deve inviare quando la connessione è pronta.
CONNECT_OK_STRING = "OK"
CONNECT_OK_TIMEOUT = 10

# Nominativo/nodo LoRa MeshCom locale da controllare nel campo dst del JSON.
MY_NODE = "IK5XMK-99"

# Il messaggio APRS-FI deve iniziare così.
APRS_PREFIX = "APRS-FI:"

# Gruppi autorizzati.
# Esempi:
#   "22251" accetta solo G22251
#   "222"   accetta solo G222
#   "222*"  accetta G222, G2221, G22251, ecc.
ALLOWED_GROUPS = ["222*", "292"]

# Formato gruppo in ingresso: APRS-FI:G22251 testo
GROUP_RE = re.compile(r"^G([0-9]{3,5})\s+(.+)$", re.IGNORECASE)

# A true rimuove un eventuale message-id APRS finale tipo: {XMK6
REMOVE_APRS_MESSAGE_ID = True

# Terminatore riga per inviare comandi alla net console.
LINE_ENDING = "\r\n"

# Prima di trasmettere, il programma attende che dalla console non arrivi nulla
# per questo numero di secondi.
CONSOLE_IDLE_BEFORE_TX_SECONDS = 2.0

# Debug semplice.
SHOW_IGNORED = True
SHOW_RAW_RX = True


# ============================================================
# FUNZIONI DI UTILITÀ
# ============================================================

def log_ignored(reason):
    """Stampa il motivo dello scarto solo se il debug è attivo."""
    if SHOW_IGNORED:
        print(f"[IGNORA] {reason}")


def extract_json_from_line(line):
    """Estrae il JSON presente dentro una riga della net console."""
    start = line.find("{")
    end = line.rfind("}")

    if start < 0 or end <= start:
        log_ignored("riga senza JSON")
        return None

    try:
        return json.loads(line[start:end + 1])
    except json.JSONDecodeError as e:
        log_ignored(f"JSON non valido: {e}")
        return None


def group_is_allowed(group):
    """Verifica se il gruppo è presente nella configurazione."""
    for rule in ALLOWED_GROUPS:
        rule = str(rule).strip()

        if not rule:
            continue

        # Regola a prefisso: 222* accetta 222, 2221, 22251, ecc.
        if rule.endswith("*"):
            prefix = rule[:-1]
            if group.startswith(prefix):
                return True

        # Regola esatta: 22251 accetta solo 22251.
        elif group == rule:
            return True

    return False


def strip_aprs_message_id(text):
    """Rimuove un eventuale message-id APRS finale, per esempio 'prova {XMK6'."""
    if not REMOVE_APRS_MESSAGE_ID:
        return text

    return re.sub(r"\s+\{[A-Za-z0-9]{1,8}$", "", text).strip()


def build_meshcom_message(frame):
    """
    Controlla il frame JSON e costruisce il messaggio in uscita.

    Ritorna una stringa pronta da inviare, oppure None se il frame non va gestito.
    """
    if frame.get("type") != "msg":
        log_ignored("frame non di tipo msg")
        return None

    dst = str(frame.get("dst", "")).strip()
    msg = str(frame.get("msg", "")).strip()

    if dst != MY_NODE:
        log_ignored(f"dst diverso dal mio nodo: {dst!r} != {MY_NODE!r}")
        return None

    if not msg.startswith(APRS_PREFIX):
        log_ignored(f"messaggio senza prefisso {APRS_PREFIX!r}")
        return None

    payload = msg[len(APRS_PREFIX):].strip()
    match = GROUP_RE.match(payload)

    if not match:
        log_ignored("formato non valido: atteso APRS-FI:Gnnn testo")
        return None

    group = match.group(1)
    text = match.group(2).strip()
    text = strip_aprs_message_id(text)

    if not group_is_allowed(group):
        log_ignored(f"gruppo non autorizzato: {group}")
        return None

    if not text:
        log_ignored("testo vuoto")
        return None

    # Formato di invio verso LoRa MeshCom.
    return f"::{{{group}}} {text}"


# ============================================================
# CONNESSIONE E INVIO
# ============================================================

def connect_to_console():
    """Apre la connessione TCP verso la net console MeshCom."""
    print(f"[INFO] Connessione a {TELNET_HOST}:{TELNET_PORT} ...")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_OK_TIMEOUT)
    sock.connect((TELNET_HOST, TELNET_PORT))

    print(f"[INFO] Connesso. Attendo {CONNECT_OK_STRING!r} ...")

    buffer = b""
    wanted = CONNECT_OK_STRING.encode("utf-8", errors="replace")
    start_time = time.time()

    while time.time() - start_time < CONNECT_OK_TIMEOUT:
        data = sock.recv(1024)
        if not data:
            raise RuntimeError("connessione chiusa durante l'inizializzazione")

        buffer += data

        try:
            print("[INIT RX]", data.decode("utf-8", errors="replace").strip())
        except Exception:
            pass

        if wanted in buffer:
            print("[INFO] Console pronta.")
            sock.setblocking(False)
            return sock

    raise TimeoutError(f"{CONNECT_OK_STRING!r} non ricevuto entro {CONNECT_OK_TIMEOUT} secondi")


def wait_console_idle(sock, already_buffered=""):
    """
    Attende che non arrivi nulla dalla console per alcuni secondi.

    Se durante l'attesa arrivano dati, vengono accodati e restituiti al main loop,
    così nessuna riga ricevuta viene persa.
    """
    buffer = already_buffered
    idle_start = time.time()

    while True:
        remaining = CONSOLE_IDLE_BEFORE_TX_SECONDS - (time.time() - idle_start)

        if remaining <= 0:
            return buffer

        readable, _, _ = select.select([sock], [], [], min(0.2, remaining))

        if not readable:
            continue

        data = sock.recv(4096)
        if not data:
            raise RuntimeError("connessione chiusa dall'host")

        decoded = data.decode("utf-8", errors="replace")
        buffer += decoded
        idle_start = time.time()

        if SHOW_RAW_RX:
            print("[RX DURANTE ATTESA]", decoded.rstrip())


def send_meshcom(sock, text):
    """Invia una riga alla net console MeshCom."""
    line = text + LINE_ENDING
    sock.sendall(line.encode("utf-8", errors="replace"))
    print(f"[TX] {text}")


# ============================================================
# PROGRAMMA PRINCIPALE
# ============================================================

def main():
    sock = connect_to_console()
    rx_buffer = ""

    print("[INFO] Bridge avviato.")
    print(f"[INFO] Nodo controllato: {MY_NODE}")
    print(f"[INFO] Gruppi autorizzati: {ALLOWED_GROUPS}")
    print("[INFO] Premi CTRL-C per uscire.")

    while True:
        readable, _, _ = select.select([sock], [], [], 0.5)

        if not readable:
            continue

        data = sock.recv(4096)
        if not data:
            raise RuntimeError("connessione chiusa dall'host")

        decoded = data.decode("utf-8", errors="replace")
        rx_buffer += decoded

        if SHOW_RAW_RX:
            print("[RX]", decoded.rstrip())

        while "\n" in rx_buffer:
            line, rx_buffer = rx_buffer.split("\n", 1)
            line = line.strip()

            if not line:
                continue

            frame = extract_json_from_line(line)
            if frame is None:
                continue

            outgoing = build_meshcom_message(frame)
            if outgoing is None:
                continue

            print(f"[OK] Messaggio valido: {outgoing}")
            print(f"[INFO] Attendo {CONSOLE_IDLE_BEFORE_TX_SECONDS:.1f}s di silenzio console prima della TX ...")

            # Durante l'attesa possono arrivare altre righe: non vengono perse.
            rx_buffer = wait_console_idle(sock, rx_buffer)
            send_meshcom(sock, outgoing)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] Uscita richiesta dall'utente.")
        sys.exit(0)
    except Exception as e:
        print(f"[ERRORE] {e}")
        sys.exit(1)
