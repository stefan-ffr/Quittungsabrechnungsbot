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
| `/personen` | Alle Personen & Salden |
| `/person_add [Name]` | Person hinzufügen |
| `/person_del [Name]` | Person entfernen |
| `/loeschen` | Letzten Eintrag rückgängig |

## Installation

### One-Liner (Debian/Ubuntu)

Installiert Docker, richtet die Konfiguration ein und startet den Bot:

```bash
curl -fsSL https://raw.githubusercontent.com/stefan-ffr/Quittungsabrechnungsbot/main/setup.sh | bash
```

Das Script führt interaktiv durch die Einrichtung (Telegram Token, AI Provider, Chat-IDs).

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

## Stack

- Python 3.12 / python-telegram-bot
- Claude API oder OpenRouter (Quittungserkennung)
- SQLite (Datenhaltung)
- Docker + Watchtower (automatische Updates bei jedem Push)
- GitHub Actions (automatischer Image-Build nach ghcr.io)
