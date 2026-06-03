# Quittungsbot

Telegram-Bot zur Quittungserfassung per Foto/PDF mit KI-gestützter Positionserkennung, Kostenteilung und Schulden-/Guthaben-Verwaltung.

## Features

- **Quittungserkennung** – Foto oder PDF senden, AI extrahiert alle Positionen automatisch
- **Zahler-Tracking** – Pro Quittung angeben wer gezahlt hat (du oder eine andere Person)
- **Kostenteilung** – Items einzeln oder gesammelt auf Personen aufteilen (gleiche Anteile)
- **Geldflüsse** – Zahlungen in beide Richtungen buchen (erhalten / gegeben)
- **Saldenverwaltung** – Übersicht wer wem wie viel schuldet
- **Inline-Personenerstellung** – Neue Personen direkt im Flow anlegen
- **Kontextabhängiges Keyboard** – Buttons passen sich dem aktuellen Schritt an
- **AI Provider wählbar** – Anthropic (direkt) oder OpenRouter

## Bot-Befehle

| Befehl | Funktion |
|---|---|
| Foto/PDF | Quittung analysieren & zuweisen |
| `/geld` | Zahlung buchen (beide Richtungen) |
| `/saldo` | Alle Salden mit Aufschlüsselung |
| `/detail [Name]` | Kontoauszug einer Person |
| `/verlauf` | Letzte Zahlungen |
| `/quittungen` | Letzte Quittungen |
| `/sync_money` | Eigene Transaktionen an Money Manager senden |
| `/personen` | Alle Personen & Salden |
| `/person_add [Name]` | Person hinzufügen |
| `/person_del [Name]` | Person entfernen |
| `/loeschen` | Letzten Eintrag rückgängig |

## Installation

### One-Liner (Debian/Ubuntu / Proxmox LXC)

Installiert Docker, richtet die Konfiguration ein und startet den Bot. Funktioniert auf jedem Debian/Ubuntu-System, auch in Proxmox LXC-Containern.

```bash
curl -fsSL https://raw.githubusercontent.com/stefan-ffr/Quittungsabrechnungsbot/main/setup.sh | bash
```

Das Script führt interaktiv durch die Einrichtung (Telegram Token, AI Provider, Chat-IDs).

> **Proxmox LXC Setup:**
>
> 1. LXC erstellen: Debian 12 oder Ubuntu 22.04 Template, mind. 512 MB RAM, 4 GB Disk
> 2. LXC-Optionen in Proxmox setzen (vor dem Start):
>    - `Features → nesting=1, keyctl=1`
>    - Oder in `/etc/pve/lxc/<ID>.conf`:
>      ```
>      features: nesting=1,keyctl=1
>      ```
> 3. Für **unprivileged** LXCs zusätzlich in der LXC-Config:
>    ```
>    lxc.apparmor.profile: unconfined
>    ```
>    Alternativ: **privileged** LXC verwenden (einfacher, nur `nesting=1` nötig)
> 4. LXC starten und Setup-Script ausführen

### Manuell

```bash
git clone https://github.com/stefan-ffr/Quittungsabrechnungsbot.git
cd Quittungsabrechnungsbot
cp .env.example .env
# .env ausfüllen (siehe Konfiguration)
docker compose up -d
```

## Konfiguration (.env)

### Telegram

| Variable | Beschreibung |
|---|---|
| `TELEGRAM_TOKEN` | Bot-Token von [@BotFather](https://t.me/BotFather) |
| `ALLOWED_CHAT_IDS` | Erlaubte Telegram Chat-IDs, kommagetrennt (via [@userinfobot](https://t.me/userinfobot)) |

### AI Provider

| Variable | Beschreibung |
|---|---|
| `AI_PROVIDER` | `anthropic` (Standard) oder `openrouter` |
| `ANTHROPIC_API_KEY` | API Key von [Anthropic](https://console.anthropic.com/) |
| `OPENROUTER_API_KEY` | API Key von [OpenRouter](https://openrouter.ai/keys) |
| `AI_MODEL` | Modell-Override (optional, z.B. `anthropic/claude-sonnet-4` für OpenRouter) |

### Money Manager (optional)

| Variable | Beschreibung |
|---|---|
| `MONEY_MANAGER_URL` | Basis-URL der Money-Manager-Instanz, z.B. `https://money.example.ch` |
| `MONEY_MANAGER_API_KEY` | API-Key (Money Manager → Einstellungen → Integrationen) |
| `CURRENCY` | Standardwährung für Ausgleichszahlungen (Default `CHF`) |

## Money Manager Integration

Optional kann der Bot seine Abrechnungen an einen
[Money Manager](https://github.com/stefan-ffr/money-manager) schicken. Der Bot bleibt die
Quelle für die detaillierte Personen-/Positions-Aufteilung; gespiegelt werden nur die
**eigenen** Geldflüsse in ein dediziertes Konto **„Quittungsabrechnung"**.

1. In Money Manager unter **Einstellungen → Integrationen** einen API-Key erstellen.
2. `MONEY_MANAGER_URL` und `MONEY_MANAGER_API_KEY` in der `.env` setzen, Bot neu starten.
3. Im Chat **`/sync_money`** ausführen – sendet die Transaktionen der aktiven Gruppe.

Der Push ist **idempotent** (über `external_ref`), wiederholtes Synchronisieren erzeugt also
keine Doppelbuchungen. Mapping (eigene Sicht):

| Bot | Money Manager |
|---|---|
| Quittung (eigener Anteil) | Ausgabe `-my_share`, `external_ref=receipt-<id>` |
| Ausgleich „erhalten" | Einnahme `+Betrag`, `external_ref=transfer-<id>` |
| Ausgleich „gegeben" | Ausgabe `-Betrag`, `external_ref=transfer-<id>` |

## Stack

- Python 3.12 / python-telegram-bot
- Claude API oder OpenRouter (Quittungserkennung)
- SQLite (Datenhaltung)
- Docker + Watchtower (automatische Updates bei jedem Push)
- GitHub Actions (automatischer Image-Build nach ghcr.io)
