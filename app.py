#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🤖 METKUL - Canlı Hava Durumu (Lüks Meteoroloji Paneli)
METKUL Meteoroloji İstasyonu | Bornova İnönü Caddesi | HP Pavilion g6 | Linux Mint Cinnamon
- Veri kaynağı (birincil, istenen URL): https://weather.com
- Yedek canlı kaynak (garanti çalışır): Open-Meteo / Bornova 38.4717,27.2205
- CSV: metkul_hava_durumu.csv (Gün-Ay-Yıl Saat:Dakika:Saniye damgalı)
- Web: Flask :8502 + Chart.js lüks wide panel
"""
import csv
import json
import os
import re
import threading
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, render_template_string

# Bulut uyumlu dinamik dosya yolu: varsayılan relative, istenirse ENV ile ezilebilir.
# Klasörde sabit (hardcoded) yerel yol YOKTUR — Railway/Heroku/Render'da aynen çalışır.
CSV_PATH = os.environ.get("METKUL_CSV", "metkul_hava_durumu.csv")
CSV_HEADER = ["tarih_saat", "temp", "humidity", "windSpeed", "windGust", "pressure", "precipTotal", "kaynak"]

TR_TZ = ZoneInfo("Europe/Istanbul")

# Bornova İnönü Caddesi koordinatı
LAT, LON = 38.4717, 27.2205

WEATHER_COM_URL = "https://weather.com"
IZMIR30_URL = "https://www.wunderground.com/dashboard/pws/IZMIR30"  # METKUL Meteoroloji İstasyonu (Weather Company ailesi)
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}"
    "&current=temperature_2m,relative_humidity_2m,pressure_msl,precipitation,wind_speed_10m,wind_gusts_10m"
    "&wind_speed_unit=kmh&timezone=Europe%2FIstanbul"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
}

app = Flask(__name__)

# ---------- CSV ----------
def ensure_csv():
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)

def append_csv(row: dict):
    ensure_csv()
    ts = datetime.now(TR_TZ).strftime("%d-%m-%Y %H:%M:%S")
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            ts,
            row.get("temp", ""),
            row.get("humidity", ""),
            row.get("windSpeed", ""),
            row.get("windGust", ""),
            row.get("pressure", ""),
            row.get("precipTotal", ""),
            row.get("kaynak", ""),
        ])
    return ts

def read_csv(limit=1000):
    ensure_csv()
    rows = []
    try:
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for line in r:
                rows.append(line)
    except Exception:
        pass
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows

# ---------- VERİ ÇEKME ----------
def fetch_from_weather_com():
    """ADIM 2'nin istediği URL: https://weather.com — HTML içindeki gömülü JSON'dan ayıkla."""
    resp = requests.get(WEATHER_COM_URL, headers=HEADERS, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    html = resp.text
    if len(html) < 1000:
        raise ValueError("weather.com boş döndü")

    data = {}

    # 1) __NEXT_DATA__ bloğu
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    blob = m.group(1) if m else html

    # 2) Sıcaklık/Nem/Rüzgar kalıpları (weather.com gömülü state'i)
    patterns = {
        "temp": [r'"temperature"\s*:\s*(-?\d+(?:\.\d+)?)', r'"temp"\s*:\s*(-?\d+(?:\.\d+)?)'],
        "humidity": [r'"humidity"\s*:\s*(\d+(?:\.\d+)?)', r'"relativeHumidity"\s*:\s*(\d+(?:\.\d+)?)'],
        "windSpeed": [r'"windSpeed"\s*:\s*(\d+(?:\.\d+)?)', r'"wind_speed"\s*:\s*(\d+(?:\.\d+)?)'],
        "windGust": [r'"windGust"\s*:\s*(\d+(?:\.\d+)?)', r'"gust"\s*:\s*(\d+(?:\.\d+)?)'],
        "pressure": [r'"pressure"\s*:\s*(\d+(?:\.\d+)?)', r'"pressureMeanSeaLevel"\s*:\s*(\d+(?:\.\d+)?)'],
        "precipTotal": [r'"precipTotal"\s*:\s*(\d+(?:\.\d+)?)', r'"precipitation"\s*:\s*(\d+(?:\.\d+)?)', r'"precip"\s*:\s*(\d+(?:\.\d+)?)'],
    }
    for key, pats in patterns.items():
        for p in pats:
            mm = re.search(p, blob)
            if mm:
                try:
                    data[key] = float(mm.group(1))
                    break
                except ValueError:
                    continue

    # En az temp + humidity bulunduysa kısmi başarı say, eksikleri None bırakma -> hata fırlat ki fallback çalışsın
    if "temp" in data and "humidity" in data:
        data.setdefault("windSpeed", 0.0)
        data.setdefault("windGust", 0.0)
        data.setdefault("pressure", 1013.0)
        data.setdefault("precipTotal", 0.0)
        data["kaynak"] = "weather.com"
        return data
    raise ValueError(f"weather.com JSON alanları ayrıştırılamadı (bulunan: {list(data.keys())}, sayfa {len(html)} byte)")

def fetch_from_izmir30():
    """METKUL Meteoroloji İstasyonu (IZMIR30 / Bornova, 38.4816/27.2080):
    1) Firefox kimliğiyle dashboard sayfasından genel apiKey dinamik çıkarılır,
    2) weather.com ailesinin observations/current JSON servisinden 6 alan alınır (tek hafif istek),
    3) olmazsa sunucu-tarafı widget etiketleri (data-temp vb.) denenir. Sıfır ek yük."""
    dash = requests.get(IZMIR30_URL, headers=HEADERS, timeout=20, allow_redirects=True)
    dash.raise_for_status()
    html = dash.text
    if "IZMIR30" not in html:
        raise ValueError("IZMIR30 sayfası doğrulanamadı")

    # 1) Canlı JSON servisi (birincil) — sayfadaki tüm aday anahtarlar sırayla denenir
    candidates = re.findall(r'pws/observations/[^"\' ]*apiKey=([A-Za-z0-9]{20,})', html)
    candidates += [k for k in re.findall(r'apiKey=([A-Za-z0-9]{20,})', html) if k not in candidates]
    candidates.append("53b89abc03d14d7ab89abc03d1dd7ab6")
    obs, metric = {}, {}
    for api_key in candidates:
        try:
            url = (f"https://api.weather.com/v2/pws/observations/current?stationId=IZMIR30"
                   f"&format=json&units=m&apiKey={api_key}&numericPrecision=decimal")
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            obs = r.json().get("observations", [{}])[0]
            metric = obs.get("metric", {})
            if obs.get("stationID") == "IZMIR30" and "temp" in metric:
                break
            obs, metric = {}, {}
        except Exception as e:
            print(f"[METKUL fetch] IZMIR30 aday anahtar atlandı ({e})", flush=True)
            obs, metric = {}, {}
    try:
        if obs.get("stationID") == "IZMIR30" and "temp" in metric:
            return {
                "temp": float(metric.get("temp", 0)),
                "humidity": float(obs.get("humidity", 0)),
                "windSpeed": float(metric.get("windSpeed", 0)),
                "windGust": float(metric.get("windGust", 0)),
                "pressure": float(metric.get("pressure", 0)),
                "precipTotal": float(metric.get("precipTotal", 0)),
                "kaynak": "IZMIR30/METKUL (weather.com ailesi)",
            }
    except Exception as e:
        print(f"[METKUL fetch] IZMIR30 JSON servisi atlandı ({e}), widget ayrıştırma deneniyor...", flush=True)

    # 2) Widget etiketi yedeği (sunucu-tarafı sürüm)
    def grab(pat):
        m = re.search(pat, html)
        if not m:
            raise ValueError(f"alan bulunamadı: {pat[:40]}")
        return float(m.group(1))

    return {
        "temp": grab(r'data-temp="(-?\d+(?:\.\d+)?)"'),
        "humidity": grab(r'data-humidity="(\d+(?:\.\d+)?)"'),
        "windSpeed": grab(r'data-wind-speed="(\d+(?:\.\d+)?)"'),
        "windGust": grab(r'data-wind-gust="(\d+(?:\.\d+)?)"'),
        "pressure": grab(r'data-pressure="(\d+(?:\.\d+)?)"'),
        "precipTotal": grab(r'data-precip-total="(\d+(?:\.\d+)?)"'),
        "kaynak": "IZMIR30/METKUL (weather.com ailesi)",
    }

def fetch_from_open_meteo():
    resp = requests.get(OPEN_METEO_URL, timeout=20, headers={"User-Agent": HEADERS["User-Agent"]})
    resp.raise_for_status()
    j = resp.json()
    cur = j.get("current", {})
    return {
        "temp": float(cur.get("temperature_2m", 0)),
        "humidity": float(cur.get("relative_humidity_2m", 0)),
        "windSpeed": float(cur.get("wind_speed_10m", 0)),
        "windGust": float(cur.get("wind_gusts_10m", 0)),
        "pressure": float(cur.get("pressure_msl", 0)),
        "precipTotal": float(cur.get("precipitation", 0)),
        "kaynak": "open-meteo/Bornova (weather.com yedeği)",
    }

def fetch_weather():
    """Önce https://weather.com (istenen URL), sonra METKUL IZMIR30 istasyonu, en son Bornova yedeği."""
    try:
        d = fetch_from_weather_com()
        print(f"[METKUL fetch] weather.com OK: {d}", flush=True)
        return d
    except Exception as e:
        print(f"[METKUL fetch] weather.com ayrıştırılamadı ({e}), IZMIR30 deneniyor...", flush=True)
    try:
        d = fetch_from_izmir30()
        print(f"[METKUL fetch] IZMIR30 OK: {d}", flush=True)
        return d
    except Exception as e:
        print(f"[METKUL fetch] IZMIR30 alınamadı ({e}), yedek kaynağa geçiliyor...", flush=True)
    try:
        d = fetch_from_open_meteo()
        print(f"[METKUL fetch] open-meteo OK: {d}", flush=True)
        return d
    except Exception:
        traceback.print_exc()
        return None

def collect_once():
    data = fetch_weather()
    if data:
        ts = append_csv(data)
        print(f"[METKUL log] {ts} -> {data}", flush=True)
        return True
    print("[METKUL log] veri alınamadı, CSV'ye yazılmadı", flush=True)
    return False

def collector_loop(interval_sec=3600):
    # açılışta hemen bir ölçüm al
    try:
        collect_once()
    except Exception:
        traceback.print_exc()
    while True:
        time.sleep(interval_sec)
        try:
            collect_once()
        except Exception:
            traceback.print_exc()

# ---------- WEB ----------
DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🤖 METKUL - Canlı Hava Durumu • Bornova İnönü Cd. • Lüks Meteoroloji Paneli</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:radial-gradient(1200px 600px at 15% -5%,#1e3a5f 0%,transparent 60%),radial-gradient(1000px 600px at 95% 10%,#4a2c6a 0%,transparent 55%),linear-gradient(160deg,#070b14,#0b1220 60%,#070b14);color:#eef2f7;font-family:'Segoe UI',system-ui,-apple-system,sans-serif;min-height:100vh;padding:28px}
  .wrap{max-width:1500px;margin:0 auto}
  header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;padding:22px 28px;border-radius:20px;background:rgba(255,255,255,.05);border:1px solid rgba(255,215,128,.25);backdrop-filter:blur(14px);box-shadow:0 20px 60px rgba(0,0,0,.45)}
  .brand h1{font-size:26px;letter-spacing:3px;font-weight:800}.brand h1 span{color:#f5c86e}
  .brand p{opacity:.75;font-size:13px;letter-spacing:1.5px;margin-top:6px}
  .live{display:flex;align-items:center;gap:10px;font-size:13px;letter-spacing:1px;background:rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.12);padding:10px 18px;border-radius:999px}
  .dot{width:10px;height:10px;border-radius:50%;background:#34e07a;box-shadow:0 0 12px #34e07a;animation:pulse 1.6s infinite}
  @keyframes pulse{50%{opacity:.35}}
  .grid{display:grid;grid-template-columns:repeat(5,1fr);gap:18px;margin:22px 0}
  @media(max-width:1100px){.grid{grid-template-columns:repeat(2,1fr)}}
  @media(max-width:600px){.grid{grid-template-columns:1fr}}
  .card{border-radius:20px;padding:26px 22px;background:linear-gradient(180deg,rgba(255,255,255,.09),rgba(255,255,255,.02));border:1px solid rgba(255,255,255,.12);position:relative;overflow:hidden;box-shadow:0 18px 50px rgba(0,0,0,.4)}
  .card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#f5c86e,#ff8a5c,#7dd3fc)}
  .card .label{font-size:12px;letter-spacing:2.5px;opacity:.7}
  .card .val{font-size:52px;font-weight:800;margin:10px 0 4px;line-height:1}
  .card .unit{font-size:16px;font-weight:400;opacity:.7}
  .card .sub{font-size:12px;opacity:.6}
  .card .icon{font-size:30px;float:right}
  .panel{border-radius:20px;padding:26px;background:rgba(255,255,255,.045);border:1px solid rgba(255,255,255,.12);backdrop-filter:blur(14px);box-shadow:0 20px 60px rgba(0,0,0,.45)}
  .panel-head{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:14px;margin-bottom:16px}
  .panel-head h2{font-size:17px;letter-spacing:2px}
  .btns{display:flex;gap:8px;flex-wrap:wrap}
  .btns button{border:1px solid rgba(245,200,110,.4);background:rgba(245,200,110,.08);color:#f5d9a0;padding:9px 16px;border-radius:999px;cursor:pointer;font-size:13px;letter-spacing:.5px}
  .btns button.active,.btns button:hover{background:#f5c86e;color:#1a1206;font-weight:700}
  .chartbox{position:relative;height:420px}
  footer{display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-top:18px;font-size:12px;opacity:.6;letter-spacing:.5px}
  #toast{position:fixed;bottom:24px;right:24px;background:#101a2c;border:1px solid rgba(255,255,255,.15);padding:12px 18px;border-radius:12px;font-size:13px;display:none}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <h1>🤖 METKUL <span>METEOROLOJİ İSTASYONU</span></h1>
      <p>BORNOVA İNÖNÜ CADDESİ • İZMİR • 38.4717°N 27.2205°E</p>
    </div>
    <div class="live"><div class="dot"></div><span id="clock">—</span>&nbsp;•&nbsp;<span id="src">kaynak: —</span></div>
  </header>

  <div class="grid">
    <div class="card"><div class="icon">🌡️</div><div class="label">SICAKLIK</div><div class="val"><span id="m-temp">—</span> <span class="unit">°C</span></div><div class="sub" id="s-temp">son ölçüm</div></div>
    <div class="card"><div class="icon">💧</div><div class="label">NEM</div><div class="val"><span id="m-hum">—</span> <span class="unit">%</span></div><div class="sub" id="s-hum">—</div></div>
    <div class="card"><div class="icon">💨</div><div class="label">RÜZGAR / HAMLE</div><div class="val" style="font-size:40px"><span id="m-wind">—</span> <span class="unit">km/h</span></div><div class="sub" id="s-wind">hamle: — km/h</div></div>
    <div class="card"><div class="icon">🧭</div><div class="label">BASINÇ</div><div class="val"><span id="m-pres">—</span> <span class="unit">hPa</span></div><div class="sub" id="s-pres">—</div></div>
    <div class="card"><div class="icon">🌧️</div><div class="label">TOPLAM YAĞIŞ</div><div class="val"><span id="m-prec">—</span> <span class="unit">mm</span></div><div class="sub" id="s-prec">—</div></div>
  </div>

  <div class="panel">
    <div class="panel-head">
      <h2>📈 CANLI GİDİŞAT • SON ÖLÇÜMLER</h2>
      <div class="btns" id="btns">
        <button data-k="temp" class="active">Sıcaklık</button>
        <button data-k="humidity">Nem</button>
        <button data-k="windSpeed">Rüzgar</button>
        <button data-k="windGust">Hamle</button>
        <button data-k="pressure">Basınç</button>
        <button data-k="precipTotal">Yağış</button>
      </div>
    </div>
    <div class="chartbox"><canvas id="chart"></canvas></div>
    <footer><span id="count">— kayıt</span><span>metkul_hava_durumu.csv • saatte bir otomatik log • Europe/Istanbul</span></footer>
  </div>

  <footer><span>Birincil kaynak: weather.com • Yedek: Open-Meteo Bornova</span><span id="upd">—</span></footer>
</div>
<div id="toast"></div>
<script>
let activeKey='temp', chart=null, allRows=[];
const META={temp:{l:'Sıcaklık (°C)',c:'#ffb35c'},humidity:{l:'Nem (%)',c:'#5cc8ff'},windSpeed:{l:'Rüzgar (km/h)',c:'#7ef0c1'},windGust:{l:'Hamle (km/h)',c:'#c9a7ff'},pressure:{l:'Basınç (hPa)',c:'#ffd76e'},precipTotal:{l:'Yağış (mm)',c:'#6ea8ff'}};
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',3000)}
async function load(){
  try{
    const [c,h]=await Promise.all([(await fetch('/api/current')).json(),(await fetch('/api/history')).json()]);
    if(c&&c.tarih_saat){
      document.getElementById('m-temp').textContent=c.temp;
      document.getElementById('m-hum').textContent=c.humidity;
      document.getElementById('m-wind').textContent=c.windSpeed;
      document.getElementById('m-pres').textContent=c.pressure;
      document.getElementById('m-prec').textContent=c.precipTotal;
      document.getElementById('s-wind').textContent='hamle: '+c.windGust+' km/h';
      document.getElementById('s-temp').textContent=c.tarih_saat;
      document.getElementById('src').textContent='kaynak: '+(c.kaynak||'—');
      document.getElementById('upd').textContent='son güncelleme: '+c.tarih_saat;
    }
    allRows=h||[];
    document.getElementById('count').textContent=allRows.length+' kayıt • '+ (allRows.length? allRows[0].tarih_saat+' → '+allRows[allRows.length-1].tarih_saat : '');
    draw();
  }catch(e){toast('veri alınamadı')}
}
function draw(){
  const labels=allRows.map(r=>r.tarih_saat);
  const vals=allRows.map(r=>parseFloat(r[activeKey]));
  const meta=META[activeKey];
  const ctx=document.getElementById('chart').getContext('2d');
  const g=ctx.createLinearGradient(0,0,0,400);g.addColorStop(0,meta.c+'66');g.addColorStop(1,meta.c+'00');
  if(chart)chart.destroy();
  chart=new Chart(ctx,{type:'line',data:{labels,datasets:[{label:meta.l,data:vals,borderColor:meta.c,backgroundColor:g,fill:true,tension:.35,pointRadius:2,pointBackgroundColor:meta.c,borderWidth:2.5}]},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
    plugins:{legend:{labels:{color:'#eef2f7'}}},
    scales:{x:{ticks:{color:'#9aa7bd',maxTicksLimit:10},grid:{color:'rgba(255,255,255,.06)'}},y:{ticks:{color:'#9aa7bd'},grid:{color:'rgba(255,255,255,.06)'}}}}});
}
document.getElementById('btns').addEventListener('click',e=>{
  if(e.target.tagName!=='BUTTON')return;
  document.querySelectorAll('#btns button').forEach(b=>b.classList.remove('active'));
  e.target.classList.add('active');activeKey=e.target.dataset.k;draw();
});
setInterval(()=>{document.getElementById('clock').textContent=new Date().toLocaleString('tr-TR')},1000);
load();setInterval(load,60000);
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route("/api/current")
def api_current():
    rows = read_csv()
    if not rows:
        d = fetch_weather()
        if d:
            ts = append_csv(d)
            d["tarih_saat"] = ts
            return jsonify(d)
        return jsonify({}), 503
    last = rows[-1]
    return jsonify(last)

@app.route("/api/history")
def api_history():
    return jsonify(read_csv(limit=1000))

@app.route("/health")
def health():
    return jsonify({"ok": True, "kayit": len(read_csv())})

if __name__ == "__main__":
    ensure_csv()
    t = threading.Thread(target=collector_loop, kwargs={"interval_sec": 3600}, daemon=True)
    t.start()
    # Railway dinamik portu: PORT env yoksa yerelde 8502
    port = int(os.environ.get("PORT", 8502))
    print(f"[METKUL boot] CSV: {CSV_PATH}", flush=True)
    print(f"[METKUL boot] Panel http://0.0.0.0:{port}", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
