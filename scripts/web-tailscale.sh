#!/usr/bin/env bash
# Chạy web Igniterra chỉ trên 127.0.0.1 và mở nó cho tailnet qua `tailscale serve` (HTTPS do Tailscale cấp).
#
#   scripts/web-tailscale.sh            # cổng 8600
#   PORT=8700 scripts/web-tailscale.sh
#
# Bắt buộc STUDIO_BASIC_AUTH="user:mật khẩu" (biến môi trường hoặc dòng trong .env): trang thuê được GPU có phí.
# Ctrl+C dừng web và gỡ cấu hình serve. Không mở ra internet công khai (đó là `tailscale funnel`, không dùng).
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-8600}"

if [[ -z "${STUDIO_BASIC_AUTH:-}" && -f .env ]]; then
  STUDIO_BASIC_AUTH="$(sed -n 's/^STUDIO_BASIC_AUTH=//p' .env | tail -n1 | sed "s/^['\"]//; s/['\"]$//")"
fi
if [[ "${STUDIO_BASIC_AUTH:-}" != *:?* ]]; then
  echo "Thiếu STUDIO_BASIC_AUTH=\"user:mật khẩu\" (đặt biến hoặc thêm vào .env). Không chạy web khi chưa có mật khẩu." >&2
  exit 1
fi
export STUDIO_BASIC_AUTH

command -v tailscale >/dev/null || { echo "Chưa cài tailscale." >&2; exit 1; }
tailscale status >/dev/null 2>&1 || { echo "Tailscale chưa kết nối; chạy: sudo tailscale up" >&2; exit 1; }

# serve cần quyền root, trừ khi đã chạy một lần: sudo tailscale set --operator=$USER
TS=(tailscale)
tailscale serve status >/dev/null 2>&1 || TS=(sudo tailscale)

cleanup() {
  "${TS[@]}" serve --https=443 off >/dev/null 2>&1 || true
  [[ -n "${WEB_PID:-}" ]] && kill "$WEB_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

uv run python -m studio web --host 127.0.0.1 --port "$PORT" &
WEB_PID=$!

for _ in $(seq 1 30); do
  curl -s -o /dev/null "http://127.0.0.1:$PORT/static/style.css" && break
  kill -0 "$WEB_PID" 2>/dev/null || { echo "Web không khởi động được." >&2; exit 1; }
  sleep 1
done

"${TS[@]}" serve --bg "$PORT"
HOST="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
echo
echo "Igniterra: https://$HOST  (chỉ máy trong tailnet; trình duyệt sẽ hỏi user/mật khẩu)"
echo "Ctrl+C để dừng."
wait "$WEB_PID"
