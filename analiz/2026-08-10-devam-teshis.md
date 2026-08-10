# Teşhis: "Düşüşte neden SHORT vermedi?" — 10 Ağustos 2026

**Kullanıcı sorusu:** Sistem haftalarca SHORT verdi (hepsi stop), sonra tam
düşüş geldiği gün SHORT VERMEDİ. Yukarıda yanlış yön, aşağıda doğru yön ama
sessiz. Neden?

## Cevap: Motor düşüşü GÖRDÜ ama teyit kapısı kesti

Düşüş penceresinde (14:00–18:20 UTC / 17:00–21:20 TR) kohort kayıtları:

- **20 aday oluştu, neredeyse hepsi SHORT, skorlar 85–100** (güçlü). Motor kör
  değildi — düşüşü net gördü.
- **Ama hepsi `kapanis_tipi = DEVAM`** (destek kırılımı / trend devamı), hiçbiri
  `DONUS` (tepeden dönüş) değildi.
- **Ve hepsinin `teyit = None`** — hiçbiri sinyale dönüşemedi.

Bütün gün: 100 SHORT adayı üretildi, sadece 10'u teyit etti — **onların hepsi
de GRAB_DONUS'tu** (gecenin yükseliş tepesindeki dönüş denemeleri, rr<2 ya da
stop). Gündüz düşüşündeki DEVAM adaylarının **tamamı teyitsiz kaldı.**

## Kök sebep: DEVAM kapısı en sıkı kapı (3/3)

Koddan doğrulandı (`_sweep_teyit`):
- **DONUS** (tepeden dönüş): 3 order-flow kriterinden **2'si** yeter (OI zorunlu).
- **DEVAM** (kırılım devamı): **3'ü de ZORUNLU** — OI artışı + delta kırılım
  yönüyle aynı + emici aynı yön (ters emici iptal eder). En sıkı kapı.

Düşüş gövdesinde OI **eriyor** (bugün −400M) — yani "OI artışı" kriteri
kırılım-devam SHORT'unda nadiren sağlanır. Sonuç: DEVAM kapısı fiilen hiç
açılmıyor; motor kırılım hareketlerini yapısal olarak sinyale çeviremiyor.

## Bu bir yapısal karakter: motor DÖNÜŞ avcısı, TREND takipçisi değil

İki hafta örüntüsünün kökü tam olarak bu:

| Piyasa fazı | Motor ne üretir | Sonuç |
| --- | --- | --- |
| Yükseliş tepesi | GRAB_DONUS SHORT (dönüş avı) | Teyit eder ama squeeze ezer → stop |
| Düşüş gövdesi | GRAB_DEVAM SHORT (kırılım) | 3/3 kapısı kesip fırsatı kaçırır |

Motor "aşırı uzadı, geri döner" mantığıyla kurulu (mean-reversion). Squeeze/
trend rejiminde bu iki kez de yanlış tarafta kalıyor: dönüşü erken avlıyor,
devamı hiç avlayamıyor.

## Bugün teyit edilseydi ne olurdu? (kaba ölçüm)

Düşüşteki DEVAM SHORT adayları teyit edilseydi yön **doğruydu**:

| Aday (UTC) | Giriş~ | Dip | O ana kâr |
| --- | --- | --- | --- |
| 15:30 (skor 100) | 64.379 | 63.811 | +0.88% |
| 16:30 (skor 95) | 64.090 | 63.811 | +0.44% |
| 17:00 (skor 100) | 63.904 | 63.813 | +0.14% |

(Kesin R yok — teyitsiz olduğu için stop/hedef hesaplanmadı; ama yön kesin doğru,
düşüş sürdü.) Erken olanlar (15:30) daha çok kazanırdı — DONUS'ların tersine.

## Öneri: "DEVAM kaçırma" serisini de ölç (yeni salt-ölçüm adayı)

Kaçırılan-av serisinin DONUS-rr kolu **aleyhte** çıkmıştı (kapı gevşetme kâr
etmiyordu). Ama bu **farklı bir kapı**: DEVAM 3/3 teyidi. Ayrı ölçmeye değer:

**Öneri (onay bekler):** Her gün DEVAM tipli, yüksek-skorlu (≥85), teyitsiz
SHORT/LONG adaylarını topla; teyit edilselerdi (giriş=kırılım kapanışı, stop=
yapısal) ne olurdu simüle et; kümülatif net R biriktir. 2-3 hafta sonra soru:
"DEVAM kapısı (3/3) fırsat mı kaçırıyor, yoksa gürültüyü mü eliyor?"

Bu, rejim ölçümüyle **birleşince** çok güçlü: "trend rejiminde DEVAM adayları
kârlı mı?" sorusu, motoru dönüş-avcısından trend-de-avlayan bir yapıya
dönüştürmenin (ya da dönüştürmemenin) kanıtı olur.

**Kod değişikliği YOK** — bu bir teşhis + ölçüm önerisi. Rejim ve MFE/MAE zaten
akıyor; DEVAM-kaçırma serisi istenirse sabah turlarına eklenir (sadece offline
hesap, motora dokunmaz).
