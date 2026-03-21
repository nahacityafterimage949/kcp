#!/usr/bin/env bash
# deploy-traffic-report.sh
# Configura e inicia o KCP Traffic Reporter no VPS.
# Uso: sudo bash deploy-traffic-report.sh
set -euo pipefail

REPO_DIR="/dados/kcp"
ENV_FILE="/etc/kcp/traffic-report.env"
SERVICE="kcp-traffic-report"

echo "=== KCP Traffic Reporter — Deploy ==="

# 1. Criar /etc/kcp se não existe
mkdir -p /etc/kcp

# 2. Copiar env example se ainda não existe (admin preenche depois)
if [ ! -f "$ENV_FILE" ]; then
    cp "$REPO_DIR/infra/traffic-report.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    chown kcp:kcp "$ENV_FILE"
    echo ""
    echo "⚠️  AÇÃO NECESSÁRIA: preencha as credenciais em $ENV_FILE"
    echo "    GITHUB_TOKEN, REPORT_EMAIL_TO, SMTP_USER, SMTP_PASS"
    echo ""
fi

# 3. Instalar unit systemd
cp "$REPO_DIR/infra/kcp-traffic-report.service" /etc/systemd/system/
systemctl daemon-reload

# 4. Validar que o env file foi preenchido
if grep -q "SUBSTITUA_SEU_TOKEN\|SEU_EMAIL\|SEU_GMAIL" "$ENV_FILE"; then
    echo "❌ $ENV_FILE ainda tem valores de placeholder."
    echo "   Edite o arquivo e rode novamente:"
    echo "   sudo nano $ENV_FILE"
    echo "   sudo systemctl restart $SERVICE"
    exit 1
fi

# 5. Habilitar e iniciar
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
sleep 2

STATUS=$(systemctl is-active "$SERVICE")
echo "$SERVICE: $STATUS"

if [ "$STATUS" = "active" ]; then
    echo ""
    echo "✅ Serviço rodando! Primeiro email será enviado agora."
    echo ""
    echo "Comandos úteis:"
    echo "  sudo journalctl -u $SERVICE -f          # logs em tempo real"
    echo "  sudo journalctl -u $SERVICE --since '1h ago'"
    echo "  sudo systemctl restart $SERVICE         # forçar envio imediato"
    echo "  python3 $REPO_DIR/infra/kcp-traffic-report.py --dry-run  # testar sem email"
else
    echo "❌ Serviço falhou. Verifique:"
    echo "  sudo journalctl -u $SERVICE -n 30"
fi
