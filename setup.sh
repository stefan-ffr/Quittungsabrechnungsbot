#!/bin/bash
set -e

REPO="stefan-ffr/Quittungsabrechnungsbot"
RAW="https://raw.githubusercontent.com/$REPO/main"
INSTALL_DIR="/opt/quittungsbot"

echo "═══════════════════════════════════════"
echo "  Quittungsbot – Installer"
echo "═══════════════════════════════════════"
echo ""

# ── Docker installieren falls nötig ──
if ! command -v docker &>/dev/null; then
    echo "📦 Docker wird installiert..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    echo "✅ Docker installiert"
else
    echo "✅ Docker bereits vorhanden"
fi

# ── Projektverzeichnis anlegen ──
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
echo "📁 Installationsverzeichnis: $INSTALL_DIR"

# ── Dateien von GitHub laden ──
echo "⬇️  Lade Konfiguration..."
curl -fsSL "$RAW/docker-compose.yml" -o docker-compose.yml
curl -fsSL "$RAW/.env.example" -o .env.example

# ── .env erstellen / aktualisieren ──
if [ -f .env ]; then
    echo ""
    echo "⚙️  .env existiert bereits."
    read -rp "  Neu konfigurieren? [j/N] " RECONFIGURE
    if [[ ! "$RECONFIGURE" =~ ^[jJyY]$ ]]; then
        echo "  → Bestehende Konfiguration beibehalten"
        SKIP_CONFIG=true
    fi
fi

if [ "$SKIP_CONFIG" != "true" ]; then
    echo ""
    echo "═══════════════════════════════════════"
    echo "  ⚙️  Konfiguration"
    echo "═══════════════════════════════════════"
    echo ""

    # ── Telegram ──
    echo "── Telegram ──"
    echo ""
    echo "  1. Öffne @BotFather in Telegram"
    echo "  2. /newbot → Namen & Username wählen"
    echo "  3. Token kopieren"
    echo ""
    read -rp "  Telegram Bot Token: " TELEGRAM_TOKEN
    echo ""
    echo "  Chat-ID herausfinden: Nachricht an @userinfobot senden"
    echo "  Mehrere IDs mit Komma trennen (z.B. 123456,789012)"
    echo ""
    read -rp "  Erlaubte Chat-IDs: " ALLOWED_CHAT_IDS
    echo ""

    # ── AI Provider ──
    echo "── AI Provider ──"
    echo ""
    echo "  1) Anthropic (direkt) – https://console.anthropic.com/"
    echo "  2) OpenRouter          – https://openrouter.ai/keys"
    echo ""
    read -rp "  Auswahl [1/2]: " AI_CHOICE

    AI_PROVIDER="anthropic"
    ANTHROPIC_API_KEY=""
    OPENROUTER_API_KEY=""
    AI_MODEL=""

    if [ "$AI_CHOICE" = "2" ]; then
        AI_PROVIDER="openrouter"
        echo ""
        read -rp "  OpenRouter API Key: " OPENROUTER_API_KEY
        echo ""
        echo "  Standard-Modell: anthropic/claude-sonnet-4"
        read -rp "  Anderes Modell? (Enter = Standard): " AI_MODEL
    else
        echo ""
        read -rp "  Anthropic API Key: " ANTHROPIC_API_KEY
        echo ""
        echo "  Standard-Modell: claude-sonnet-4-20250514"
        read -rp "  Anderes Modell? (Enter = Standard): " AI_MODEL
    fi

    # ── .env schreiben ──
    cat > .env <<EOF
# Telegram
TELEGRAM_TOKEN=$TELEGRAM_TOKEN
ALLOWED_CHAT_IDS=$ALLOWED_CHAT_IDS

# AI Provider: anthropic oder openrouter
AI_PROVIDER=$AI_PROVIDER
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
OPENROUTER_API_KEY=$OPENROUTER_API_KEY
AI_MODEL=$AI_MODEL

# Pfade (Docker-intern)
DATABASE_PATH=/data/receipts.db
UPLOAD_PATH=/data/uploads
EOF

    echo ""
    echo "✅ .env erstellt"
fi

# ── Container starten ──
echo ""
echo "🚀 Starte Container..."
docker compose pull
docker compose up -d

echo ""
echo "═══════════════════════════════════════"
echo "  ✅ Quittungsbot läuft!"
echo "═══════════════════════════════════════"
echo ""
echo "  Verzeichnis:  $INSTALL_DIR"
echo "  Konfig:       $INSTALL_DIR/.env"
echo "  Logs:         cd $INSTALL_DIR && docker compose logs -f"
echo "  Stoppen:      cd $INSTALL_DIR && docker compose down"
echo "  Neustarten:   cd $INSTALL_DIR && docker compose restart"
echo "  Neu konfig.:  Dieses Script erneut ausführen"
echo ""
echo "  Watchtower aktualisiert automatisch alle 5 Min."
echo ""
