# 🐋 Balina Avcısı (Whale Hunter)

BTC/USDT için **çok-borsalı order-flow** analiz motoru. İki beyin birlikte çalışır:

1. **Scalp beyni (v7.x)** — Binance/Bybit/OKX emir defterleri + Coinalyze çok-borsalı
   CVD/OI/funding/likidasyon verisiyle katmanlı skorlama; **LONG / SHORT / BEKLE** üretir.
2. **Swing LIQ GRAB motoru (v8–v9)** — seviye haritası üzerinde 15dk kapalı mumla
   likidite süpürmesi (sweep) avlar; order-flow teyidi ve R/R kapısından geçen
   kurulumları **kağıt üstü** swing sinyali olarak kaydeder.

Her şey Supabase'e yazılır; sistem kendi sinyalini geriye dönük ölçer (self-backtest).

> Bu bir **analiz/sinyal** aracıdır, otomatik emir göndermez. Finansal tavsiye değildir.

---

## Mimari (thread'ler)

`main.py` tek dosyada, birden çok daemon thread ile çalışır:

| Thread | Görev |
| --- | --- |
| `web_sunucu_calistir` | Sağlık kontrolü + panel rotaları (Render'ı uyanık tutar) |
| `rest_yardimci_guncelle` | Binance/Bybit/OKX emir defteri + funding + OI (REST, 60 sn) |
| `coinalyze_guncelle` | Coinalyze CVD/OI/likidasyon (60 sn) + funding/L-S **5 turda bir** (v9.2 kadans) + 15dk/1s/4s kline beslemesi (GRAB) |
| `websocket_calistir` | Binance vadeli `aggTrade` + `bookTicker` akışı |
| `likidasyon_websocket_calistir` | Binance `!forceOrder@arr` likidasyon akışı |
| `spot_websocket_calistir` | Binance spot `bookTicker` akışı |
| `adaptif_esik_guncelle` | Geçmiş veriden derinlik/likidasyon/CVD/volatilite eşikleri (10 dk) |
| `geri_test_dongusu` | Çoklu ufuk (15/30/60/240 dk) isabet + kohort backfill |
| `ozet_ve_analiz_dongusu` | **Ana döngü** — scalp skoru + swing motoru + tüm kayıtlar (60 sn) |

## Scalp beyni (katman hiyerarşisi)

- **Katman 2 — İŞLEMLER / CVD** → çekirdek belirleyici
- **Katman 3 — ABSORBSİYON** → duvar ↔ işlem ilişkisi
- **Katman 1 — DUVAR** → spoof'a açık; tek başına sinyal üretemez
- **Katman 4 — OI/FUNDING** → kırılganlık vetosu (squeeze/tasfiye rejiminde keser)

Sinyal şartı: skor ≥ **90** + taraflar arası marj ≥ **25** + VE-kapıları
(`islem/direnc`) + süreç ailesi. **v9.7 (kullanıcı kararı — Faz 2):** `duvar` ve
`hedef` (maliyet çıtası) kapıları **kaldırıldı** — order book 60sn REST
fotoğrafıdır, hiçbir karara girmez; toplama/kayıt/panel ve `ob_olcum` hakemleri
yaşar. Skor eşiği geçip kapıya takılanlar **gölge sinyal** olarak kaydedilir
(v9.3 — `golge_yon/golge_kapi/golge_skor`; VERİDİR, işlem çağrısı değildir).
Scalp aktif sinyali susturulmuştur (`SCALP_SINYAL_AKTIF=False`) — skorlar ve
kayıt akışı sürer.

## Swing LIQ GRAB motoru (v8 → v9.4)

Boru hattı, hepsi **15dk kapalı mum** disipliniyle (`son_islenen_15dk_ts`):

1. **Seviye haritası** — kaynak başına güç puanı (ELLE 40, ÇOKZAMAN 25, LIQ 20,
   EQ 20, VP 15, ROUND 10, PIVOT 5); birleşen seviyelerde güç devri; kalıcılık izi
   (`ilk_gorulme_ts`, `yenileme_sayisi`). Kapıya giren asgari güç: **40**.
2. **Sweep adayı** — delme eşiği `max(SWEEP_MIN_DELME_PCT, 0.2×ATR15)` + mumun
   seviyeyi gerçekten kesmesi + likidasyon eşliği + seviye başına 90dk cooldown.
3. **Kapanış kararı** — aynı kapalı mumda `DONUS` (fitilli tuzak) / `DEVAM`
   (gerçek kırılım) / belirsiz.
4. **Order-flow teyidi** — DONUS: 2/3 (OI zorunlu); DEVAM: 3/3. v9.7: emici
   kanıtının defter-eğilim izi karar dışı; CVD tabanlı parça (rejim+tükenme) kanıt.
   Kademe SINYAL şartı 3/3 (emici şartı ve yön uzlaşısı karar dışı, kayıtta).
5. **Sinyal** — stop: fitil ucu / kırılan seviye ± `max(0.0005×fiyat, 0.1×ATR15)`;
   kısa hedef: yönde ilk güçlü seviye; swing hedef: karşı likidite havuzu.
   **R/R kapısı (v9.2, birleşik): `rr_kisa ≥ 2.0`** — grab ve kademe yolunun
   ikisinde de; `rr_swing` salt kayıt.

**Güçlendiriciler (sadece kayıt):** FVG (3 ardışık mum), CHoCH (16 mumda
mühürlenir), EQ kümeleri (zaman dilimi başına).

**Ölçüm/teşhis:** dakikalık teşhis kolonları (delme belirleyeni, likidite
yoğunlukları, `mum_ici_konum`, donma sayacı, harita özeti), reddedilen adaylar
(`GRAB_ADAY`) ve N+1 ters kapanış kohortu (`GRAB_ADAY_N1`) — teyit kapısı
veriyle kalibre edilir. Kohort budaması **gerçek sinyalleri korur** (v9.4).

## Faz 1 disiplini (davranış garantisi)

- `v72_taban_main.py` **donmuş referanstır — asla güncellenmez.** Kabul suite'i
  500 rastgele girdide skor+sinyalin tabanla birebir aynı olduğunu kanıtlar
  (`fark=0`); swing motoru scalp yoluna dokunamaz.
- Sıfır tuzağı: ölçülemeyen değer **None** yazılır, asla 0 uydurulmaz.
- Yeni ölçümler önce **salt kayıt** olarak eklenir; kapı/karar değişiklikleri
  ancak biriken canlı veriyle gerekçelendirilir.

## Testler

```bash
python test_v73_kabul.py     # 247 kabul testi — hepsi geçmeli ("HEPSI GECTI")
```

Suite: 500'lük eşdeğerlik + süpürme/emilim/tasfiye vakaları + v8 motor adımları
+ güçlendiriciler + v8.8–v10.1 ölçüm sözleşmeleri. Testler gerçek kod bloklarını
marker'la çıkarıp çalıştırır (ikiz mantık yok).

## Paneller ve rotalar

| Rota | Dosya | Not |
| --- | --- | --- |
| `/` | — | rota listesi + sağlık kontrolü |
| `/kokpit` | `v4balina_swing_kokpit.html` | masaüstü swing kokpiti |
| `/mobil`, `/kokpit-mobil` | `v4balina_swing_kokpit_mobil.html` | mobil kokpit (5sn görünürlük-korumalı yenileme) |
| `/panel` | `v3balina_sonar_terminal.html` | masaüstü scalp paneli |
| `/mobil-eski` | `balina_mobil.html` | eski telefon paneli (arşiv) |

Tüm panel dosyaları bu repodadır. Mobil kokpit, masaüstünden **üreteçle**
türetilir (style+script bayt-bayt aynı; tek fark yoklama kadansı) — masaüstü
değişince mobil yeniden üretilmelidir.

## Ortam değişkenleri

| Değişken | Zorunlu | Açıklama |
| --- | --- | --- |
| `SUPABASE_URL` | ✅ | Supabase proje URL'i |
| `SUPABASE_KEY` | ✅ | Supabase service/anon anahtarı |
| `COINALYZE_API_KEY` | ⛔️ opsiyonel* | Coinalyze API anahtarı. Yoksa toplulaştırılmış veri çekilmez ve CVD güvenilir sayılmaz → skor üretilmez |
| `PORT` | ⛔️ | Web sunucu portu (Render otomatik verir; varsayılan 8080) |

\* Anahtar yoksa sistem çalışır ama veri kalite kapısı CVD'yi reddeder; anlamlı sinyal için Coinalyze şarttır.

## Veri modeli (Supabase)

- **`balina_avcisi_data`** — dakikalık arşiv (~67 kolon): core akış + swing
  kolonları + teşhis kolonları + `golge_*`. Yeni kolonlar `ALTER TABLE ...
  ADD COLUMN IF NOT EXISTS` ile açılır; kod, kolon yokken ana kaydı asla
  düşürmez (best-effort ayrı UPDATE'ler).
- **`balina_ayarlar`** — JSONB anahtarları: `swing_seviyeler_elle/oto`,
  `swing_karar`, `swing_kohortu` (olaylar; gerçek sinyaller budamadan korunur),
  `swing_kohort_istatistik`, `grab_aktif_sinyal`, `tasfiye_kohortu`,
  `ve_kapisi_redleri`, `geri_test_istatistik`, `surec_durumu`, `motor_sabitleri`.

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

Ardından `http://localhost:8080/` rota listesini açar.

## Render dağıtımı

`render.yaml` ile tek tıkla dağıtılabilir (Blueprint). `main` dalı otomatik
deploy olur; akış: çalışma dalı (`swing/faz-a-seviye-haritasi`) → PR → merge.
Ortam değişkenlerini Render panelinden `sync: false` alanları için girin.
Sağlık kontrolü `/` rotasıdır.

## Sürüm özeti

| Sürüm | Ne |
| --- | --- |
| v7.x | Scalp beyni + süreç hafızası + emilim/tasfiye + kohortlar |
| v8 | LIQ GRAB swing motoru (5 adım) + FVG/CHoCH/EQ güçlendiricileri |
| v8.8–v9.0 | Teşhis enstrümantasyonu: aday kayıtları, N1 kohortu, donma izi, harita özeti |
| v9.1 | Çift emici şerit paneli + 429 sertleştirme |
| v9.2 | **Birleşik R/R kapısı (`rr_kisa≥2`)** + Coinalyze funding/L-S kadansı |
| v9.3 | Gölge sinyal görünürlüğü (3 kolon, salt kayıt) |
| v9.4 | Kohort budaması gerçek sinyalleri korur |
| v9.5 | Uzun ufuk (1-4 gün) + rejim dilimli geri-test (salt ölçüm) |
| v9.6 | Order book değer ölçümü — duvar-uyum + gölge-duvar hakemleri |
| v9.7 | **Order book karardan çıkarıldı** (duvar/hedef kapıları + emici şartı; kayıt yaşar) |
| v9.8 | Teşhis paketi: BV dışlama yönü, skor faktör ayrıştırma, zirve histogramı, izle/gir sayacı, sinyalsizlik panosu (salt ölçüm) |
| v9.9 | Hedef mesafesi kova ölçümü — kaldırılan maliyet çıtasının "hayaleti" (salt ölçüm) |
| v10.0 | **Yapışık-seviye atlama** (kısa hedef ≥1R uzak) + kanaat kalıcılık dilimi ölçümü |
| v10.1 | Sinyal kartı **akıbet izleme** (durum STOP/HEDEF, salt ölçüm) + BTCUD.A sembol kara listesi |
| v10.2 | **Rejim ölçümü** (squeeze/trend/range etiketi — sinyal hangi ortamda doğdu) + sinyal **MFE/MAE** (ölü giriş ↔ kâr-verip-aldı ayrımı). Salt ölçüm, karar-dışı |

`yedek/` klasöründe tarihli veri anlık görüntüleri tutulur.
