#!/bin/bash
# 🤖 METKUL - Canlı Hava Durumu - başlatma betiği (terminal kapansa da çalışır)
cd "$(dirname "$0")"
nohup python3 app.py > panel.log 2>&1 &
echo "METKUL başlatıldı PID:$! -> http://localhost:8502"
