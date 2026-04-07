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

# ── .env erstellen falls nicht vorhanden ──
if [ -f .env ]; then
    echo "⚙️  .env existiert bereits – wird beibehalten"
else
    echo ""
    echo "⚙️  Konfiguration einrichten:"
    echo ""

    read -rp "  Telegram Bot Token: " TELEGRAM_TOKEN
    read -rp "  Anthropic API Key:  " ANTHROPIC_API_KEY
    read -rp "  Erlaubte Chat-IDs (kommagetrennt): " ALLOWED_CHAT_IDS

    cat > .env <<EOF
TELEGRAM_TOKEN=$TELEGRAM_TOKEN
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
ALLOWED_CHAT_IDS=$ALLOWED_CHAT_IDS
DATABASE_PATH=/data/receipts.db
UPLOAD_PATH=/data/uploads
EOF
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
echo "  Logs:         docker compose -f $INSTALL_DIR/docker-compose.yml logs -f"
echo "  Stoppen:      docker compose -f $INSTALL_DIR/docker-compose.yml down"
echo "  Updaten:      docker compose -f $INSTALL_DIR/docker-compose.yml pull && docker compose -f $INSTALL_DIR/docker-compose.yml up -d"
echo "  (Watchtower aktualisiert automatisch alle 5 Min.)"
echo ""
