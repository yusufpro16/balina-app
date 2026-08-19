# VWAP + POC Araştırması (offline, 17 Ağu'ya hazırlık)

Kullanıcı isteği (12 Ağu): VWAP/POC kod entegrasyonu 17 Ağu'da; o zamana kadar
offline çalış, boş durma. Bu dosya her turda büyür — 17 Ağu'da karar için kanıt.

## Veri kaynağı çözüldü

- Binance **451** (coğrafi kısıt, proxy US). **OKX çalışıyor** — `BTC-USDT-SWAP`
  5dk kline, hacim dahil (`history-candles`, 100 bar/istek, `after` ile sayfalama).
- Bu, motorun arşivinde OLMAYAN gerçek trade hacmini verir → VWAP/POC hesaplanabilir.

## Bulgu 1: VWAP kullanıcının mantığını BİREBİR doğruladı

Kullanıcının 12 Ağu SHORT'u — penceresi **15:20–17:00 TR** (12:20–14:00 UTC).
Gün-içi VWAP (00:00 UTC reset, typical=(H+L+C)/3, hacim-ağırlıklı):

| Saat (TR) | Kapanış | VWAP | Fiyat−VWAP |
| --- | --- | --- | --- |
| 15:20–15:30 | 64.2–64.4 | ~63.95 | **+255…+460** (CPI whipsaw, VWAP üstü) |
| 15:45 | 63.961 | 63.975 | −14 (ilk temas) |
| 15:50–16:15 | ~64.0 | ~63.98 | ±100 (VWAP'ta çırpınma) |
| **16:20→** | 63.936↓ | 63.978 | **−42, −47, −79…** (VWAP altına YERLEŞTİ) |
| 17:00 | 63.641 | 63.965 | **−324** (kesin kırılım) |

**Senin "VWAP üstüne çıkıp sonra altına attı" gözlemin tam doğru:** fiyat
15:20–16:15 VWAP üstünde/civarında çırpındı (CPI gürültüsü), **16:20'de VWAP
altına kesin yerleşti ve bir daha üstüne çıkamadı** = breakdown teyidi. Giriş
~63.900, VWAP o an ~63.978 → fiyat VWAP'ın hemen altında. Mantık kusursuz.

## Bulgu 2: POC ~kalibre (yöntem farkı, ince ayar sürüyor)

Rolling POC (son N bar hacim profili, en yoğun fiyat):
- Çoğu pencerede (4–36s, 10$ kova) POC = **64.240** — senin r.POC'un (64.112)
  128 USD yakınında. Kavram aynı; fark TradingView'in tick-bazlı ince hesabı vs
  benim 10$ kovam + typical-price seçimi.
- Her hâlükârda: 63.900 giriş hem VWAP (63.978) hem POC (64.11–64.24) **altında**
  = satıcı bölgesi. Setup'ın iki ayağı da teyitli.

**Yapılacak:** kova boyutunu (10$→1$) ve fiyat-referansını (typical→close, ya da
gerçek tick) TradingView r.POC ile eşleştir. Sonraki tur.

## Bulgu 3: Motor bu işlemi neden veremedi (12 Ağu vaka ile birleşik)

Motor tam bu pencerede 16 SHORT DEVAM adayı üretti (13:15'te 63.950 — girişe 50
puan), hepsini 3/3 teyit kapısı kesti. Kullanıcının VWAP+POC araçları breakdown'ı
net gösterirken, motorun order-flow teyidi (OI artışı şartı) düşüşte sağlanmadı.

## Yol haritası (17 Ağu'ya kadar, her tur ilerler)

1. ✅ Veri kaynağı (OKX) + VWAP hesabı + kullanıcı işlemi doğrulama
2. ⏳ POC yöntemini TradingView r.POC ile tam kalibre et
3. ⏳ **Sistematik test:** son 30 günde "fiyat VWAP+POC üstünden altına kesin
   kırılım" (ve tersi) kurulumlarını bul → 1/2/4 saat sonrası net R → kârlı mı?
   Gerçekçi stop (min risk %0.15) + maliyet.
4. ⏳ Motorun DEVAM adaylarıyla çaprazla: "VWAP+POC teyitli DEVAM adayları,
   3/3 order-flow yerine daha iyi bir filtre mi?"
5. → 17 Ağu: kanıt olgunsa VWAP+POC ölçüm/sinyal spec'i öner (kod, onayla).

**Kod değişikliği YOK — saf offline araştırma.** OKX verisi + hesaplar
`scratchpad/`'de; bulgular bu dosyada birikiyor.

---

## Tur 2 (13 Ağu akşam — motor billing-askıda, offline devam)

**İlk sistematik test: ham VWAP kırılımı yön-tahmin edici mi?**
OKX 5dk, son 5 gün (1500 bar, 8–13 Ağu). Her VWAP kesişiminde (aşağı=SHORT,
yukarı=LONG) sonraki 1/2/4 saat lehte getiri:

| Yön | Ufuk | n | İsabet | Maliyetli net |
| --- | --- | --- | --- | --- |
| SHORT | 60/120/240dk | ~53 | %37–49 | −0.02…−0.08% |
| LONG | 60/120/240dk | ~52 | %50–61 | −0.12…−0.16% |

**Sonuç: ham VWAP kırılımı TEK BAŞINA kârsız** (isabet ~%50, maliyet sonrası
hepsi negatif). Sebep net: 5 günde 105 kırılım = VWAP çok sık kesiliyor
(fiyat VWAP civarında salınırken her whipsaw bir "sinyal"). Gürültü.

**Ders — kullanıcının setup'ı ham kırılım DEĞİL:** Onun 12 Ağu işlemi
"VWAP üstünde bir süre kal (CPI whipsaw) → KESİN altına yerleş (16:20, bir daha
üstüne çıkmadı) → POC de altında → olay-high stop" idi. Yani güç **seçicilikte**:
whipsaw filtresi + POC çift-teyidi + kesin-yerleşme. Ham kesişim bunların hiçbirini
taşımıyor.

**Sonraki tur:** setup'ı seçici tanımla — (a) VWAP altında N bar KALMA (whipsaw
ele), (b) POC de altında (çift teyit), (c) gün-içi ilk kesin kırılım — ve bu
seçici versiyonu aynı 5 günde yeniden test et. Ham vs seçici farkı, setup'ın
gerçek edge'ini gösterecek.

---

## Tur 3 (14 Ağu — motor döndü, VWAP/POC devam)

**Seçici setup testi: whipsaw filtresi + POC çift-teyidi ham kırılımı kârlıya çevirdi mi?**

Tanım (kullanıcının 12 Ağu mantığı): VWAP kırılımı + sonraki **3 bar (15dk)
VWAP'ın öbür tarafında KALMA** (whipsaw ele) + o an **POC'nin de öbür tarafında**
(çift teyit); giriş=teyit barı kapanışı, stop=son 12 bar swing high/low + tampon,
hedef=2R, dar-stop filtresi (min risk %0.15). Aynı 5 gün (OKX, 8–13 Ağu).

| | n | Kazanan | STOP | net R | ort/setup |
| --- | --- | --- | --- | --- | --- |
| **Ham kırılım (Tur 2)** | 105 | ~%50 | — | kârsız (maliyetli negatif) | ~0 |
| **Seçici (Tur 3)** | **27** | 11 | 16 | **+4.44R** | **+0.164R** |

**Whipsaw filtresi çalıştı:** 105 ham kesişimden 27 seçici setup kaldı (gürültünün
%74'ü elendi). Ve kalan setuplar **net pozitif** (+4.44R). Kullanıcının setup
mantığı (seçicilik + POC teyidi) ham kırılımı kârlıya çeviriyor — **ilk somut
kanıt** ki VWAP/POC'nin edge'i seçicilikte.

**DÜRÜST UYARILAR (RR deneyi hakem dersleri):**
- n=27, **tek hafta** (8–13 Ağu düşüş ağırlıklı — 20 SHORT/9 LONG). Sonuç
  rejim-koşullu olabilir: düşüş haftasında SHORT'lar doğal kârlı.
- **Maliyet dahil değil** (2R/−1R idealize dolum; gerçek stop kayması +
  komisyon netR'yi düşürür).
- İşaret, kanıt değil. 30+ gün + maliyet modeli + farklı rejim gerekli.

**Sonraki tur:** (a) maliyet + gerçekçi stop dolumu ekle, (b) 30 güne genişlet
(OKX sayfalama), (c) motorun DEVAM adaylarıyla çaprazla. 17 Ağu'da bu olgunlaşırsa
"VWAP+POC teyit ölçümü" spec'i güçlü aday.

---

## Tur 4 (15 Ağu — maliyet eklendi, KRİTİK sentez)

Tur 3 seçici setup'a (+4.44R idealize) **gerçekçi maliyet** (%0.10 g-d) eklendi:

| | idealize | maliyetli |
| --- | --- | --- |
| Seçici VWAP+POC (n=27) | +4.44R | **−3.86R** |

Medyan stop %0.32 (dar) → maliyet 0.31R/setup, 27×0.31≈8.3R → pozitif kayboldu.
**RR deneyiyle aynı duvar: stop-darlığı + maliyet öldürüyor.**

**AMA kullanıcının +1.22R'si GENİŞ stop (CPI-high %0.67) kullandı → maliyet_R
sadece ~0.15.** Yani setup'ın değeri VWAP/POC seçiciliği + GENİŞ/yapısal stop
kombinasyonunda. Dar swing-stop maliyette batıyor, geniş olay-stop yenebilir.

**Sonraki (Tur 5):** aynı seçici setup'ı GENİŞ stop (gün-high/low veya
olay-yapısal) ile test et. Dar vs geniş stop farkı = setup'ın gerçek edge testi.
Bu, 17 Ağu spec kararının merkez sorusu.

---

## Tur 5 (15 Ağu akşam — GENİŞ stop testi, MERKEZ SORU cevaplandı)

Tur 4'ün merkez sorusu: aynı seçici VWAP+POC setup'ı, dar swing-stop yerine
**geniş yapısal stop** (gün-içi high/low = kullanıcının olay-high mantığının
sistematik karşılığı) ile maliyeti yenebilir mi? Aynı 5 gün (OKX, 8–13 Ağu),
aynı seçici tanım, tek fark stop yeri; maliyet %0.10 g-d dahil.

| Stop tipi | n | Kazanan | idealize | **maliyetli net** | medyan risk |
| --- | --- | --- | --- | --- | --- |
| **Dar** (swing 12 bar, Tur 3-4) | 27 | 11 | +4.44R | **−3.86R** | %0.32 |
| **Geniş** (gün-içi high/low) | 29 | 14 | +10.39R | **+4.69R** | %0.63 |

**MERKEZ SORU CEVABI: EVET.** Geniş yapısal stop, seçici VWAP+POC setup'ını
**maliyet sonrası pozitif** yapıyor (+4.69R), dar stop ise maliyette batıyor
(−3.86R). Fark tek değişkenden: stop genişliği medyan riski %0.32→%0.63'e
çıkarıyor → işlem başına maliyet_R 0.31→~0.16'ya iniyor → aynı hareket daha az
maliyet yiyor. Ayrıca geniş stop daha az erken-vuruş (kazanan 11→14) sağlıyor.

**Üç ipin sentezi artık sayısal olarak kapandı:**
1. Ham VWAP kırılımı kârsız (Tur 2) — gürültü.
2. Seçicilik (whipsaw + POC teyidi) idealde kârlı ama DAR stop'ta maliyet öldürür
   (Tur 3-4): +4.44R → −3.86R.
3. **Seçicilik + GENİŞ yapısal stop maliyeti yener (Tur 5): +4.69R.**
   Bu tam kullanıcının 12 Ağu +1.22R işleminin yapısı (VWAP/POC seçici giriş +
   CPI-high geniş stop). Sistematik test, sezgisel işlemi doğruladı.

**DÜRÜST UYARILAR (değişmedi, hatta önemi arttı):**
- n=29, **tek hafta** (8–13 Ağu düşüş ağırlıklı). Geniş stop düşüş trendinde
  SHORT'a doğal avantajlı — rejim-koşullu olabilir. Farklı rejimde (RANGE/yükseliş)
  tekrar gerekli.
- Geniş stop **mutlak R kaybını büyütür** (yanlışta −1R ama o −1R daha çok $).
  Pozisyon boyutu sabit-risk ile ayarlanmalı; "geniş stop bedava" değil.
- 5 gün + tek setup ailesi. 30 gün + maliyet duyarlılığı (%0.05–0.15) + motor
  DEVAM adaylarıyla çapraz hâlâ gerekli.

> **NOT (Tur 7, 19 Ağu):** Mekanik kol Tur 6'da kapandı. Tek geçerli bağlam
> **haber-olayı** (kullanıcının 12 Ağu CPI SHORT'u). Bugün 19 Ağu FOMC (21:00 TR).
> Bu akşam izlenecek seviyeler (OKX, sabah hesabı): **VWAP 64.396, rolling POC
> 64.970, gün H64.713/L64.170.** Fiyat 64.186 = **VWAP ve POC'nin ALTINDA** (satıcı
> bölgesi — kullanıcının setup yapısı). FOMC penceresinde (18:00 UTC): fiyat VWAP'ı
> (64.396) reddederse SHORT-teyidi; üstüne çıkıp tutunursa LONG-kayması. Stop
> referansı = FOMC-high (kullanıcı mantığı). Akşam nabzında CANLI gözlem — sinyal
> değil, olay-bağlamı ölçümü.

**Sonraki (Tur 6):** (a) 30 güne genişlet (OKX sayfalama) — tek-hafta rejim
riskini kır; (b) maliyet duyarlılık taraması (%0.05/0.10/0.15) — hangi maliyette
edge kayboluyor; (c) motorun DEVAM adaylarıyla çaprazla — "geniş-stop + VWAP/POC
teyitli DEVAM" motorun 3/3 kapısına gerçek alternatif mi? 17 Ağu spec kararı için
bu üç ayak tamamlanmalı. **Karar netleşiyor: eğer spec önerilecekse çekirdeği
"seçici VWAP/POC giriş + geniş yapısal stop" olmalı, dar-stop değil.**

---

## Tur 6 (16 Ağu — 30 GÜNE GENİŞLETİLDİ → Tur 5 sonucu ÇÜRÜDÜ (dürüst düzeltme))

**Ana bulgu: Tur 5'in "geniş stop kazanıyor" (+4.69R) sonucu SAĞLAM DEĞİLDİ.**
33 günlük veriye (OKX, 14 Tem–16 Ağu, 9.500 bar) genişletince, aynı seçici setup
her stop tipinde ve her alt-pencerede **NEGATİF** çıktı.

### 30 günlük sonuç (maliyet %0.10)
| Stop | n | Kazanan | idealize | maliyetli | medyan risk |
| --- | --- | --- | --- | --- | --- |
| Dar | 138 | 50 | **−18.37R** | **−58.14R** | %0.41 |
| Geniş | 169 | 72 | **−11.65R** | **−49.98R** | %0.60 |

Maliyetsiz idealizede bile ikisi de negatif → setup, mekanize hâliyle out-of-sample
yön tahmin etmiyor.

### KRİTİK: Tur 5'in kendi penceresi bile üretilemedi
Aynı yeni (dikkatli) pipeline'la 8–13 Ağu'yu (Tur 5 penceresi) tek başına çalıştırdım:
| Alt-pencere | n | idealize | maliyetli |
| --- | --- | --- | --- |
| **8–13 Ağu (Tur 5'in penceresi)** | 33 | **−6.06R** | **−14.23R** |
| 14 Tem–7 Ağu | 125 | −1.92R | −28.52R |
| 14–16 Ağu | 11 | −3.66R | −7.23R |
| Tüm 33 gün | 169 | −11.65R | −49.98R |

Tur 5, **aynı pencerede +4.69R** demişti; yeni pipeline aynı pencerede **−14.23R**
veriyor. Fark rejim değil, **uygulama kırılganlığı**: eski Tur 5 script'i (kayıp
scratchpad'de) muhtemelen temiz 2R/−1R dolum + farklı ufuk/POC kovası kullandı;
yeni pipeline gerçekçi (4s ufuk + çözülmeyende mark-to-market). İşareti uygulama
detayına göre ters dönen bir setup = **edge değil.**

### EN SAĞLAM KANIT: first-touch (sınırsız ufuk, maliyet YOK)
Ufuk/MTM/maliyet seçimlerinden bağımsız tek net ölçüm — 2R hedef mi 1R stop mu
önce vurulur (breakeven WR %33.3):
| Stop | Kazanan | Kaybeden | **WR** | net(2R/−1R) | Karar |
| --- | --- | --- | --- | --- | --- |
| Dar | 38 | 96 | **%28.4** | −20R | EDGE YOK |
| Geniş | 52 | 108 | **%32.5** | −4R | EDGE YOK (marjinal) |

**İkisi de breakeven'in altında.** Geniş stop WR'yi %28.4→%32.5 çekiyor ama hâlâ
%33.3'ün altında → geniş stop "daha az kötü" yapıyor, **edge yaratmıyor**. Herhangi
bir maliyet eklenince ikisi de batıyor.

### DÜRÜST SONUÇ — ölçüm sahte bir edge'i yakaladı
1. Mekanik seçici VWAP/POC kırılım setup'ının **out-of-sample edge'i YOK**
   (first-touch WR breakeven altı, her iki stop).
2. Tur 5'in +4.69R'si **tek-hafta + uygulama kırılgan** artefaktıydı; düzeltildi.
3. **Stop-genişliği edge kaldıracı DEĞİL** — sadece kaybı yumuşatıyor. "Geniş stop
   çözer" hipotezi 30 günde çöktü.
4. Kullanıcının 12 Ağu **+1.22R'si geçerli ama mekanize edilemez**: o işlem
   bağlama dayanıyordu (CPI katalizörü + VWAP-reddi konvansiyonu + yüksek konviksiyon)
   — benim mekanik proxy'im günde ~5 kez ateşliyor (33 günde 169), onun nadir
   diskresyoner setup'ı değil. Mekanizasyon o bağlamı yakalayamıyor.
5. Bu, **RR deneyinin dersinin 3. tekrarı**: umut veren tek-hafta backtest →
   30 gün + first-touch + maliyette ölüyor. Meta-ders artık güçlü: **HİÇBİR setup'a
   30g + first-touch + maliyet üçlüsü olmadan inanma.**

### 17 Ağu spec kararı (Tur 6 sonrası — DÜZELTİLDİ)
**Mekanik VWAP/POC giriş sinyali ÖNERME.** Edge yok. En fazla VWAP/POC, motorun
mevcut DEVAM adaylarına **pasif bağlam etiketi** olabilir (VWAP'ın hangi tarafında
+ POC'ye mesafe), ve özellikle **haber-olay pencereleriyle** çaprazlanmalı (kullanıcının
edge'i haber-katalizörlüydü). Standalone tetikleyici asla. Bu, ölçüm-önce disiplininin
zaferi: 4 tur boyunca olgunlaşan "umut verici" bir yön, 5. turda geniş veriyle
sahte çıktı — kod yazılmadan yakalandı.
