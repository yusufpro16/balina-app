# Vaka: Kullanıcının VWAP+POC SHORT'u — motorun kör noktası kanıtlandı

**12 Ağu 2026, CPI sonrası.** Kullanıcı elle SHORT girdi ve kârda. Bu vaka,
motorun neden aynı işlemi veremediğini birebir gösteriyor.

## Kullanıcının işlemi (mantık sağlam, kârda)

- **Giriş ~63.900:** fiyat VWAP'ın üstüne çıkıp reddedildi + r.POC'nin (64.112)
  altına kırıldı → satıcı kontrolü. Klasik **VWAP-reddi + POC-kırılımı** breakdown.
- **Stop = CPI high (~64.327):** olay yüksekliği stop referansı (risk 427 puan).
- **Güncel 63.378 → +522 puan = +1.22R kârda.** Mantık temiz, R/R sağlıklı.

## Motor ne yaptı? GÖRDÜ ama 3/3 kapısı KESTİ

CPI sonrası (13:00–19:00 UTC) kohort: motor tam bu bölgede **16 SHORT DEVAM
adayı** üretti — **hepsi teyit=None** (kapı kesti):

| Saat (UTC) | Skor | Seviye | Tip | Teyit |
| --- | --- | --- | --- | --- |
| 13:15 | 65 | **63.950** | DEVAM | None |
| 13:45 | 70 | 63.850 | DEVAM | None |
| 14:15 | 75 | 63.618 | DEVAM | None |

13:15'teki 63.950 DEVAM SHORT, kullanıcının 63.900 girişine **50 puan** yakın.
Motor kullanıcıyla neredeyse aynı yeri, aynı yönü gördü — ama **DEVAM teyit kapısı
(3/3: OI artışı + delta + emici) düşüşte sağlanmadığı için hepsini kesti.** Bu,
10 Ağu DEVAM-kaçırma teşhisinin canlı, kullanıcı-kâr-kanıtlı örneği.

## Neden kullanıcının araçları yakaladı, motorunki kaçırdı

İki farklı dil:

| | Kullanıcı | Motor |
| --- | --- | --- |
| Sinyal | VWAP reddi + POC kırılımı (momentum/breakdown) | Seviye süpürme + order-flow teyidi (dönüş) |
| Stop | Olay yüksekliği (CPI high) | Fitil/seviye ± tampon |
| Karakter | Trend-takip | Mean-reversion (dönüş avcısı) |

Motorda **VWAP yok** ve **POC pasif** (VP seviyesi olarak var ama kırılım-teyidi
değil). Kullanıcının setup'ı tam da motorun kör olduğu boyut: gün-içi adil değer
(VWAP) + hacim değeri (POC) momentum'u.

## Öneri: VWAP + POC'yi ölç (önce veri, sonra sinyal)

**Engel:** Arşivde gerçek trade hacmi YOK (sadece `liquidation_pool_volume`).
VWAP hacim-ağırlıklı olduğu için önce **veri** gerekiyor.

**Aşama 1 (kod, küçük — salt kayıt):** Coinalyze kline hacmi zaten motora
geliyor (grab için); bunu + gün-içi VWAP + POC mesafesini arşive yaz. Ayrıca her
sinyal/aday için "VWAP'ın hangi tarafında + POC'ye mesafe" etiketi. Karar-dışı.

**Aşama 2 (2-3 hafta sonra):** Kullanıcının setup'ını sistematik test:
"VWAP-reddi + POC-kırılımı olan DEVAM adayları, olmayan­lardan daha mı kârlı?"
Bu, DEVAM-kaçırma serisiyle çaprazlanır.

**Aşama 3 (kanıt varsa):** Yeni sinyal ailesi — motorun dönüş-avcısı karakterine
**breakdown/momentum** boyutu. VWAP+POC teyidi, 3/3 order-flow kapısına
alternatif bir teyit yolu olabilir (DEVAM'ı kilitleyen OI-artışı şartını aşar).

## Bağlam: bu, üç ipin birleştiği nokta

1. **DEVAM-kaçırma** (10 Ağu): motor kırılımları 3/3 kapısıyla kaçırıyor.
2. **Rejim** (10 Ağu): squeeze/trend rejiminde dönüş-avı yanlış taraf.
3. **VWAP/POC** (12 Ağu, bu vaka): motorun momentum araçları yok.

Üçü aynı yere işaret ediyor: **motor mean-reversion; trend/breakdown boyutu
eksik.** Kullanıcının bugünkü +1.22R'lik işlemi bu eksiğin canlı bedeli/kanıtı.

**Kod değişikliği YOK.** VWAP/POC ölçüm altyapısı, kullanıcı onayıyla Aşama 1
olarak eklenebilir — ama önce hangi verinin mevcut olduğunu (kline hacmi motora
geliyor mu, arşive yazılabilir mi) doğrulamam gerekir. Sonraki adım o.
