# 🐋 Balina Avcısı (Whale Hunter)

BTC/USDT için **çok-borsalı order-flow** analiz motoru. Binance, Bybit ve OKX
emir defterlerini + Coinalyze'ın çok-borsalı toplulaştırılmış verisini (CVD, OI,
funding, likidasyon) canlı toplar; katmanlı bir skorlama beyni ile **LONG /
SHORT / BEKLE** sinyali üretir ve her şeyi Supabase'e yazar. Kendi sinyalinin
isabetini geriye dönük ölçer (self-backtest).

> Bu bir **analiz/sinyal** aracıdır, otomatik emir göndermez. Finansal tavsiye değildir.

---

## Mimari (thread'ler)

`main.py` tek dosyada, birden çok daemon thread ile çalışır:

| Thread | Görev |
| --- | --- |
| `web_sunucu_calistir` | Sağlık kontrolü + `/mobil` ve `/panel` HTML panellerini servis eder (Render'ı uyanık tutar) |
| `rest_yardimci_guncelle` | Binance/Bybit/OKX emir defteri + funding + OI (REST, 60 sn) |
| `coinalyze_guncelle` | Coinalyze çok-borsalı CVD/OI/funding/likidasyon (60 sn) |
| `websocket_calistir` | Binance vadeli `aggTrade` + `bookTicker` akışı |
| `likidasyon_websocket_calistir` | Binance `!forceOrder@arr` likidasyon akışı |
| `spot_websocket_calistir` | Binance spot `bookTicker` akışı |
| `adaptif_esik_guncelle` | Geçmiş veriden derinlik/likidasyon/CVD eşiklerini uyarlar (10 dk) |
| `geri_test_dongusu` | Çoklu ufuk (15/30/60/240 dk) isabet + `is_win` ölçümü |
| `ozet_ve_analiz_dongusu` | **Ana döngü** — skorlar, sinyal üretir, Supabase'e yazar (60 sn) |

### Skorlama beyni (katman hiyerarşisi)

- **Katman 2 — İŞLEMLER / CVD** → çekirdek, baskın belirleyici
- **Katman 3 — ABSORBSİYON** → duvar ↔ işlem ilişkisi (fiyat direnci)
- **Katman 1 — DUVAR** → spoof'a açık; tek başına sinyal üretemez (işlem teyidi şart)
- **Katman 4 — OI/FUNDING** → kırılganlık vetosu (short-squeeze / tasfiye rejiminde sinyali keser)

Ek olarak: veri kalite kapısı, CVD ıraksama, süreç hafızası (dağıtım/toplama
olgunluğu + tükenme), borsa bazlı `bv` sağlık filtresi, VE-kapısı disiplini ve
cooldown. Kod içi yorumlar sürüm sürüm (v2 → v6) bu kararların gerekçesini anlatır.

---

## Ortam değişkenleri

| Değişken | Zorunlu | Açıklama |
| --- | --- | --- |
| `SUPABASE_URL` | ✅ | Supabase proje URL'i |
| `SUPABASE_KEY` | ✅ | Supabase service/anon anahtarı |
| `COINALYZE_API_KEY` | ⛔️ opsiyonel* | Coinalyze API anahtarı. Yoksa toplulaştırılmış veri çekilmez ve CVD güvenilir sayılmaz → skor üretilmez |
| `PORT` | ⛔️ | Web sunucu portu (Render otomatik verir; varsayılan 8080) |

\* Anahtar yoksa sistem çalışır ama veri kalite kapısı CVD'yi reddeder; anlamlı sinyal için Coinalyze şarttır.

### Supabase tabloları

Kod şu tabloları bekler:

- **`balina_avcisi_data`** — her dakikanın anlık görüntüsü + skorlar + sinyal + `is_win`
- **`balina_ayarlar`** — anahtar/değer ayarlar (sembol önbelleği, süreç durumu, geri test istatistiği, VE-kapısı redleri)

---

## Yerel çalıştırma

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export SUPABASE_URL="https://xxxx.supabase.co"
export SUPABASE_KEY="..."
export COINALYZE_API_KEY="..."      # opsiyonel ama önerilir

python main.py
```

Ardından `http://localhost:8080/` sağlık sayfasını, `/mobil` ve `/panel`
panellerini açar.

> **Not:** `/panel` → `v3balina_sonar_terminal.html` bu repoda **var** (v7,
> motorla senkron yorum katmanı: pencere-yerel adaptif eşikler, 8 kapı, Sinyal
> Otopsisi). `/mobil` → `balina_mobil.html` ise repoda **yok**; eklemezseniz o
> rota 404 döner (motor yine de sorunsuz çalışır).

---

## Render dağıtımı

`render.yaml` ile tek tıkla dağıtılabilir (Blueprint). Ortam değişkenlerini
Render panelinden `sync: false` alanları için girin. Sağlık kontrolü `/`
rotasıdır; Render servisi 7/24 uyanık tutar.
