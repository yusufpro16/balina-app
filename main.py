import json
import time
import os
import bisect   # v9.5: uzun ufuk ileri-fiyat aramasi (sirali listede O(log n))
import threading
import logging
import datetime
import calendar
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import websocket  # websocket-client kütüphanesi
from supabase import create_client, Client

# =========================================================================
# KURULUM: pip install websocket-client requests supabase
# requirements.txt'e "websocket-client" eklemeyi UNUTMAYIN.
# =========================================================================
#
# ==========================  v2 UPGRADE NOTU  ============================
# Bu sürümde OMURGA (veri toplama, WebSocket, Coinalyze, önbellek, adaptif
# eşik) AYNEN korundu. Değişen tek şey: sinyalleri BİRLEŞTİREN yorumlama
# beyni (ozet_ve_analiz_dongusu içindeki skor bloğu) baştan kuruldu.
#
# Eski beynin 4 kök sorunu ve çözümü:
#   1) LONG skoru matematiksel olarak 85'e ulaşamıyordu (tavan 65, expiry
#      dışında sinyal İMKANSIZDI). -> Skor artık dereceli (0-100) ve gerçekçi
#      bir eşikte (65) tetikleniyor.
#   2) Absorbsiyon +50 katı bir "VE-kapısıydı", neredeyse hiç ateşlenmiyordu.
#      -> Absorbsiyon artık bir SÜREÇ olarak, son N dakikadaki DEĞİŞİMDEN
#      (fiyat/CVD/duvar eğimi) dereceli hesaplanıyor.
#   3) Sistem anlık fotoğraf bakıyordu, değişim değil. -> Rolling pencere
#      eklendi; artık "fiyat düşerken duvar emiyor mu" ilişkisi ölçülüyor.
#   4) Emir yaşı yön sinyali olarak overfit'ti. -> Artık YÖN sinyali değil,
#      SPOOFING FİLTRESİ; genç dev duvar sahte sayılıp skoru düşürür,
#      olgun duvar absorbsiyon skorunu güçlendirir.
#
# Ek olarak: borsa ayrışması (Binance vs Bybit ayrı delta), OI rejimi
# (short-squeeze tespiti), spot/vadeli teyidi, funding kalabalıklık
# bağlamı EKLENDİ. Ve is_win kolonunu dolduran GERİ TEST döngüsü eklendi
# (sistem artık kendi sinyalinin isabetini ölçebiliyor).
#
# ==========================  v3 UPGRADE (A + B)  =========================
# A) VERİ KALİTE KAPISI (veri_kalitesi_degerlendir):
#    - CVD sadece Coinalyze'dan (cok-borsali, USD) gelirse GÜVENİLİR sayilir.
#      WS bookTicker yedegi 10.000x olcek farkli + yanlis proxy -> REDDEDİLİR.
#    - Bozuk OI (<1e9), fiyat yok, bayat veri (>90sn) -> o dakika skor URETMEZ.
#    - Ham veri yine tabloya yazilir (analiz icin), ama long/short skor=0,
#      sinyal=BEKLE, rejim=VERI_GUVENSIZ. Cop veriyle ASLA sinyal cikmaz.
#
# B) KATMAN HİYERARŞİSİ (balina_skoru_hesapla):
#    Order flow katmanlari EŞİT DEĞİL. Yeni agirliklandirma:
#      Katman 2 (İŞLEMLER/CVD) = CEKIRDEK, us 0.50 (baskin belirleyici).
#      Katman 3 (ABSORBSİYON/direnc) = us 0.25 (teyit).
#      Katman 1 (DUVAR) = us 0.25 AMA "duvar vetosu": islem akisi yoksa
#        (satis/alis_yogunlugu<0.15) duvar NOTR sayilir -> tek basina
#        sinyal URETEMEZ (spoofing korumasi).
#      Katman 4 (OI) = VETO: squeeze/tasfiye rejiminde sinyali KESER
#        (skoru SINYAL_ESIGI altina ceker), sadece zayiflatmaz.
#
# ========================  v3.5 UPGRADE (C + D + E)  =====================
# C) CVD IRAKSAMA sinyali (_cvd_iraksama_hesapla):
#    Spot ve vadeli CVD ayni yone mi bakiyor? Uyum [-1,1] olarak olculup
#    skora dogrudan carpan olur (teyit +%18'e kadar guclendirir, iraksama
#    -%18'e kadar zayiflatir). "Vadeli itiyor spot onaylamiyor" artik sayisal.
#
# D) OKX order book EKLENDİ (3. borsa derinligi):
#    - rest_yardimci OKX BTC-USDT-SWAP defterini ceker (kontrat->BTC cevrimi).
#    - Uc borsa (Binance/Bybit/OKX) deltasi AYRI; borsa mutabakati artik
#      2/3 veya 3/3 uzerinden (daha guclu spoofing dayanikliligi).
#    - aktif_borsa<2 ise duvar teyit esigi yukselir (tek borsa spoof'a acik).
#
# E) Coinalyze 'bv' DOĞRULAMASI (coinalyze_bv_dogrula):
#    Baslangicta bir kez, Binance'in kendi taker-buy orani ile Coinalyze
#    bv/v oranini karsilastirir. Yakinsa formul girdisi GÜVENİLİR loglanir,
#    saparsa UYARI verir. CVD formulunun temelini kanitlar.
# =========================================================================

# --- LOGLAMA ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- SUPABASE BAĞLANTISI (Anahtarlar ortam değişkeninden) ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL ve SUPABASE_KEY ortam degiskenleri tanimli degil!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

SYMBOL = "btcusdt"  # WebSocket stream'lerinde küçük harf kullanılır

# --- COINALYZE (GERÇEK ÇOK-BORSALI AGGREGATION) ---
COINALYZE_API_KEY = os.environ.get("COINALYZE_API_KEY")
COINALYZE_BASE = "https://api.coinalyze.net/v1"

# =========================================================================
# PAYLAŞILAN CANLI DURUM (State)
# =========================================================================
class CanliDurum:
    def __init__(self):
        self.lock = threading.Lock()
        self.anlik_fiyat = 0.0
        self.funding_rate = 0.0001
        self.open_interest = 0.0

        self.trade_gecmisi = deque()        # 15dk pencere (VADELİ tick-rule yedek)
        self.son_tick_fiyat = 0.0
        self.spot_trade_gecmisi = deque()   # SPOT tick-rule yedek
        self.spot_son_tick_fiyat = 0.0

        # Emir defteri aynaları
        self.bids = {}
        self.asks = {}
        self.bybit_bids = {}
        self.bybit_asks = {}
        self.okx_bids = {}       # D: 3. borsa derinligi
        self.okx_asks = {}
        # v7.7: PERP defter bayatlik damgalari (spot ile simetrik). REST cekimi
        # basarisiz olursa eski defter SESSIZCE oy vermeye devam etmesin diye
        # her borsanin son basarili cekim zamani tutulur. FAZ 1: sadece OLCULUR
        # (mutabakat sayaci); skor yolu (order_book_depth_*) DEGISMEZ.
        self.perp_bids_zaman = 0.0     # Binance perp (fapi/depth)
        self.bybit_perp_zaman = 0.0    # Bybit perp (linear)
        self.okx_perp_zaman = 0.0      # OKX perp (SWAP)

        # ===== v7.5: SPOT ORDER BOOK =====
        # NEDEN: absorbsiyonun YONUNU ayirt eden en guclu gozlem, EMEN TARAFIN
        # HANGI BORSADA oldugudur. Coin BIRIKTIREN balina SPOT alir; perp'te bid
        # koymak envanter degil, kaldiracli bahistir. Sistemde bugune kadar SADECE
        # vadeli defter vardi (Binance/Bybit/OKX futures) -> "spot bid'i kalin mi"
        # sorusu SORULAMIYORDU ve toplama/dagitim ayrimi vekil metriklere kaliyordu.
        # MALIYET: +1 REST cagrisi/dk (Binance spot depth, agirlik 50). Mevcut REST
        # dongusu zaten 60sn; Binance limiti 2400 agirlik/dk -> ban riski YOK.
        self.spot_bids = {}
        self.spot_asks = {}
        self.spot_ob_zaman = 0.0   # son basarili spot defter cekimi (bayatlik kontrolu)
        # v7.6: COK-BORSALI SPOT — tek borsa (Binance) spoof'a acikti. Bybit+OKX
        # spot defteri eklendi; mutabakat (kac borsada spot bid-agir) sinyali
        # saglamlastirir (uc borsada birden ayni yonde durmak zordur).
        self.bybit_spot_bids = {}; self.bybit_spot_asks = {}; self.bybit_spot_zaman = 0.0
        self.okx_spot_bids = {}; self.okx_spot_asks = {}; self.okx_spot_zaman = 0.0

        self.likidasyonlar = deque()
        # Emir yaşı takibi (artık spoofing filtresi olarak kullanılıyor)
        self.buyuk_bid_ilk_gorulme = {}
        self.buyuk_ask_ilk_gorulme = {}

        # COINALYZE aggregated
        self.agg_liq_long = 0.0
        self.agg_liq_short = 0.0
        self.agg_open_interest = 0.0
        self.agg_funding = 0.0
        self.agg_ls_ratio = 0.0
        # v8.6: Binance global hesap L/S orani (Coinalyze L/S bos donunce fallback)
        self.binance_ls_ratio = 0.0
        self.binance_ls_zaman = 0.0
        self.agg_spot_cvd = 0.0
        self.agg_vadeli_cvd = 0.0
        self.coinalyze_saglikli = False
        self.coinalyze_cvd_saglikli = False
        self.coinalyze_cvd_zaman = 0.0   # FIX3: son BASARILI CVD hesabinin ani (bayatlik kapisi)
        self.coinalyze_liq_zaman = 0.0   # v8: son BASARILI likidasyon cekiminin ani (grab izi)

        # Adaptif eşikler
        self.esik_derinlik = 45_000_000.0
        self.esik_likidasyon = 179_000.0
        self.esik_cvd_negatif = -2000.0
        self.esik_cvd_pozitif = 2000.0
        self.esik_guncelleme_zamani = 0
        self.esik_veri_sayisi = 0

        # ================= v2 YENİ: ROLLING TARİHSEL SERİ =================
        # Beynin "değişim" görebilmesi için her dakikanın anlık görüntüsü
        # burada tutulur. Absorbsiyon bir SÜREÇTİR; tek dakika değil, son
        # birkaç dakikanın eğilimi ölçülür.
        # Her eleman: dict(ts, fiyat, bid_d, ask_d, bnb_delta, byb_delta,
        #                   vadeli_cvd, spot_cvd, oi)
        self.gecmis_seri = deque(maxlen=150)  # son ~2.5 saat (SÜREÇ hafızası için)

        # ============ v4 YENİ: SÜREÇ HAFIZASI ============
        # Anlik pencere "su an ne oluyor" der; surec hafizasi "bu KACINCI
        # dakikasinda, ne kadar OLGUNLASTI, TUKENME belirtisi var mi" der.
        # Bir dagitim/toplama sureci saatlerce surer; bitisini gormek icin
        # surecin BASINDAN beri nerede oldugunu bilmek gerekir.
        self.surec_rejim = "NOTR"        # su an devam eden surecin turu
        self.surec_baslangic = 0.0       # bu surec ne zaman basladi (epoch)
        self.surec_baslangic_fiyat = 0.0 # surec basindaki fiyat
        self.surec_baslangic_spotcvd = 0.0
        self.surec_zirve_fiyat = 0.0     # surecteki en yuksek fiyat (dagitim icin)
        self.surec_dip_fiyat = 0.0       # surecteki en dusuk fiyat (toplama icin)
        self.surec_olgunluk = 0.0        # 0-1: surec ne kadar ilerledi
        self.surec_tukenme = 0           # kac tukenme sinyali belirdi (0-4)
        self.son_sinyal_zamani = 0.0     # v5: cooldown icin son LONG/SHORT ani
        self.son_sinyal_yonu = ""        # v5: son sinyalin yonu

        # ============ v6: ÖLÇÜM ALTYAPISI ============
        # Iki hafta sonra "sistem calisiyor mu" sorusuna KANITLA cevap verebilmek
        # icin, su an her seyi kaydediyoruz. Sinyal uretimi DEGISMIYOR;
        # sadece olcum genisliyor.
        self.bv_dislanan_sayac = {}      # {sembol: kac_kez_dislandi}
        self.bv_toplam_tur = 0           # kac tur CVD hesaplandi
        self.bv_dislanan_tur = 0         # kac turda en az 1 borsa dislandi
        self.son_ve_red = ""             # son VE-kapisi red sebebi (DB'ye yazilir)

        # ============ v7.3: TASFIYE AYRIMI + SUPURME ============
        # Tick fiyat hafizasi: dakikalik ornekleme 1-dk'dan kisa fitili KACIRIR.
        # aggTrade zaten akiyor; (ts_ms, fiyat) tutup gercek fitil dibini okuruz.
        self.tick_fiyat_gecmisi = deque()   # 15dk pencere, aggTrade fiyatlari
        # Adaptif birimler (adaptif_esik_guncelle uretir):
        self.esik_volatilite = 0.0          # medyan |5dk fiyat degisimi %| (0=henuz yok)
        self.esik_lik_long_medyan = 0.0     # agg_long_liq'in SIFIR-OLMAYAN medyani
        self.esik_lik_short_medyan = 0.0    # agg_short_liq'in SIFIR-OLMAYAN medyani
        # v7.4: SPOT CVD adaptif esigi (vadeli icin zaten var; spot icin YOKTU).
        # Spot ve vadeli AYNI olcekte degil (medyan |spot|/|vadeli| ~5x) -> ayri esik.
        self.esik_spot_negatif = -2000.0
        self.esik_spot_pozitif = 2000.0
        # Likidite seviyeleri (24s swing dip/tepe; adaptif dongu uretir):
        self.likidite_dipler = []           # [{'fiyat','test','yas_dk'},...]
        self.likidite_tepeler = []
        # Supurme durum makinesi: {seviye_fiyat: {...durum...}} (SupurmeTakipci yonetir)
        self.supurme_dip_durumlari = {}
        self.supurme_tepe_durumlari = {}
        # v8.0/v8.1 SWING: seviye haritasi (adaptif_esik 10dk yazar) + karar (ozet dk)
        self.swing_seviyeler = []
        self.swing_karar = {}
        self.swing_son_kademe = 'YOK'   # v8.2: rising-edge (SINYAL kohort kaydi bir kez)
        self.son_swing_kohort_ts = {}   # v8.7: yon-bazli cooldown (SINYAL titremesi coklu kayit acmasin)
        # v8: LIQ GRAB motoru — 15dk kapali mum tabani
        self.mumlar_15dk = []           # Coinalyze 15min KAPALI mumlar (coinalyze thread yazar)
        self.pivotlar_1s = []           # 1saat pivotlar [{'fiyat','tur','ts'}] (saatte bir yenilenir)
        self.pivotlar_4s = []           # 4saat pivotlar (ayni kadans)
        self.son_islenen_15dk_ts = 0.0  # ayni 15dk mumu iki kez ISLEME (GK-8)
        self.grab_cooldown = {}         # {round(seviye): son_aday_epoch} — SWEEP_COOLDOWN_DK
        # v8: grab kohort bekleyen-olay tamponu — Supabase gecici hatasi (503/timeout)
        # dakika satiri SINYAL'e UPDATE edildikten SONRA kohort olayini DUSUREMEZ;
        # tampon bir sonraki 15dk mumunda yeniden denenir (tasfiye kohortu kalibi).
        self.grab_kohort_bekleyen = []
        # v8.8-C: N mumunda DEVAM/None siniflanan adaylar — N+1 kapanisinda olculur
        # (GRAB_ADAY_N1). Tek mum omru; restart'ta bosalir (eksik > uydurma).
        self.grab_n1_bekleyen = []
        # v8.8-E: likidasyon donma tespiti (ayri alanlar; karar zincirine girmez)
        self.lik_pencere_damgasi = None
        self.lik_borsa_sayisi = None
        self.lik_donma_sayaci = 0
        # Kohort tekrar-yazim korumasi (rising-edge):
        self.son_tasfiye_kohort_ts = {"LONG": 0.0, "SHORT": 0.0}
        # Kohort bekleyen-olay tamponu: Supabase gecici hatasi (503/timeout)
        # ONAYLI'ya gecmis nadir olayi DUSUREMEZ — tampon sonraki turda yeniden
        # denenir, yalnizca dogrulanmis yazimda temizlenir. (ozet thread'ine ozel)
        self.kohort_bekleyen = []

        self.son_guncelleme = time.time()

    def tick_min(self, gecmis_sn):
        """Son gecmis_sn saniyedeki GERCEK en dusuk islem fiyati (fitil dibi).
        Dakikalik ornekleme fitili kacirir; bu fonksiyon kacirmaz.
        Sagdan tarar, kesimde durur: 15dk'lik deque'i kilit altinda bastan sona
        suzmek WS handler'larini gereksiz bekletirdi."""
        kesim_ms = (time.time() - gecmis_sn) * 1000
        en = 0.0
        with self.lock:
            for t, f in reversed(self.tick_fiyat_gecmisi):
                if t < kesim_ms:
                    break
                if f > 0 and (en == 0.0 or f < en):
                    en = f
        return en

    def tick_max(self, gecmis_sn):
        """Son gecmis_sn saniyedeki GERCEK en yuksek islem fiyati (fitil tepesi)."""
        kesim_ms = (time.time() - gecmis_sn) * 1000
        en = 0.0
        with self.lock:
            for t, f in reversed(self.tick_fiyat_gecmisi):
                if t < kesim_ms:
                    break
                if f > en:
                    en = f
        return en

durum = CanliDurum()

BUYUK_EMIR_ESIGI_USDT = 500_000.0
EMIR_OLGUNLUK_SANIYE = 300
ESIK_GUNCELLEME_ARALIGI = 600
MIN_KAYIT_ADAPTIF = 100

# v2 YENİ: skorlama parametreleri
PENCERE_DK = 5            # değişim kaç dakikalık pencerede ölçülsün
# ================== v7 — CVD ÖLÇEK DÜZELTMESİ (FIX1/FIX3) ==================
# FIX1: CVD eşiği artık SEVİYE değil, 5-dk DEĞİŞİM (delta) dağılımından türetilir.
# Delta dağılımının magnitüdüne bir taban koyarak (payda çökmesi -> saturasyon)
# "sessiz -> gürültülü" ters arızasını engelleriz. Taban esas olarak veriye
# görecelidir (median|dL|); bu mutlak taban yalnızca ölü-piyasa güvenlik ağıdır
# (BTC-hacim birimi; ölü piyasada bastırmak DOĞRU yöndür).
CVD_ESIK_MUTLAK_TABAN = 1.0
# FIX3: son BAŞARILI Coinalyze CVD hesabı bu saniyeden eskiyse kaynak GÜVENSİZ
# sayılır (donmuş CVD ile sinyal üretimini engeller; ~4 Coinalyze döngüsü).
CVD_BAYATLIK_SN = 240
# ================== v5 — BALİNA DİSİPLİNİ ==================
# Veri kanıtı (1753 kayıt, 29 saat): skor 65-85 arası sinyaller yazı-tura
# (%47-53), skor 95+ sinyaller %71 isabetli. Sonuç: sistem çok konuşuyordu.
# Balina gibi: acele yok, her harekete tepki yok; tüm koşullar hizalanmadan
# tek kelime yok. Nadir ama nokta atışı.
SINYAL_ESIGI = 90.0       # 65 -> 90: sadece en güçlü kurulumlar konuşur
SINYAL_MARJI = 25.0       # kazanan taraf ezici üstün olmalı (flip-flop imkansız)
SINYAL_COOLDOWN_SN = 1800 # bir sinyalden sonra 30dk sus (ayni hareketi 6 kez sinyalleme)
MALIYET_CITASI_PCT = 0.30 # kurulum en az %0.30 hareket vaat etmeli (maliyet ~%0.10'un 3 kati)
# ================== v7.1 — HEDEF KAPISI KALİBRASYONU ==================
# GERÇEK VERİ (balina_avcisi_data + ve_kapisi_redleri): 'hedef' kapısı skoru
# 90'ı geçen 10 kurulumun 10'unu da blokluyordu. Sebep: 'ciddi duvar' eşiği
# BUYUK_EMIR_ESIGI_USDT ($500k) idi ve BTC defterinde her an fiyata ~%0.02-0.05
# mesafede $500k+ konsensüs duvarları var -> en yakın bariyer daima <%0.30 ->
# kapı YAPISAL OLARAK hep kapalı. $500k bir bariyer değil (saniyede yeniyor).
# 578 kayıt üzerinde ölçülen geçiş oranı: $500k->%0.3, $5M->%2.8, $10M->%22.5,
# $15M->%85.6. $10M, kapıyı gerçek filtre tutarken (%77.5 blok) en iyi
# kurulumların ~%30'unu geçiriyor — "nadir ama nokta atışı"na uygun.
# NOT: Yalnızca 'hedef' kapısının bariyer tanımını büyütür; duvar teyidi
# (Katman 1) ve likidite haritası ETKİLENMEZ (onlar BUYUK_EMIR_ESIGI_USDT'de kalır).
HEDEF_DUVAR_ESIGI_USDT = 10_000_000.0
# v7.2 — YAKIN-BÖLGE DIŞLAMA: gerçek veri, "en yakın ciddi duvar"ların %98.7'sinin
# fiyatın %0.10 İÇİNDE olduğunu gösterdi (medyan %0.030; bir örnekte duvar fiyatın
# $8 altındaydı = en iyi alış bölgesinin ta kendisi). Spread'e yapışık kotasyon
# yığını YAPISAL bariyer değildir — defterin normal şeklidir ve her an oradadır;
# onu bariyer saymak kapıyı yapısal bir KAPALI anahtara çevirir. Maliyet yarıçapı
# (~%0.10 gidiş-dönüş) içindeki duvarlar bu yüzden bariyer sayılmaz; kapı ilk
# YAPISAL bariyeri ([%0.10, %0.30) bandındaki ciddi duvar) arar ve onu bulursa
# bloklar. HEDEF_YAKIN_BOLGE_PCT < MALIYET_CITASI_PCT olmalı, yoksa kapı hiç bloklamaz.
HEDEF_YAKIN_BOLGE_PCT = 0.10

# ================== v7.3 — TASFIYE AYRIMI + SUPURME ==================
# HICBIR ESIK MUTLAK SAYI DEGILDIR. Hepsi adaptif birimlerin katsayisidir.
# Katsayilar ilk tahmindir; FAZ 1 kohortu bunlari kalibre edecek.
#
# BILINEN RISKLER (spec §10 — silme):
# 1) Geriye-donuk onyargi: mekanizma tek dogrulanmis vakadan (1 Tem 2026) fark
#    edildi. Faz 1'in amaci bu onyargiyi kirmak. Kohort negatif -> kalip COPE.
# 2) Yanlis-pozitif tabani bilinmiyor: her gercek KIRILMA da "delme" ile baslar;
#    diken carpani (2.5x) tahmindir. Ham metrikler bu yuzden kohorta yazilir.
# 3) Ornekleme kaybi: dakikalik dongu saniyelik fitili kacirir -> tick_min/tick_max
#    (asagida) gercek fitil dibini tutar. Atlanirsa kohort YANLI olur.
# 4) Tek borsa fitili spoof olabilir -> aktif_borsa ham metriklere yazilir.
# 5) COZUNURLUK TUZAGI: spot CVD 15dk grafikte diple cakisik gorunur, 5dk'da
#    10 SAAT gecikmeli cikti. Spot/OI bu yuzden GIRIS kapisi DEGIL, gecikmeli
#    olcumdur (kohortta izlenir).
# 6) OI dususunun iki anlami: fitildeki DIKEY dusus = tasfiyenin kendisi
#    (mekanik, girise dahil); sonraki saatlerin YAVAS erimesi = tuzaktaki
#    short'larin kapanmasi (yakit, gecikmeli olcum). Karistirilmaz.
TASFIYE_AYRIMI_AKTIF = False      # FAZ 1: KAPALI. Sinyal davranisi BIREBIR ayni.
SUPURME_TESPIT_AKTIF = True       # tespit + kayit acik (davranis degistirmez)

# --- A) TASFIYE AYRIMI ---
TASFIYE_DIKEN_CARPANI = 2.5       # yon-bazli likidasyon >= kendi medyaninin 2.5 kati -> DIKEN
TASFIYE_OI_MIN_PCT = 0.05         # ayni pencerede OI en az bu kadar dusmus olmali (veto esigiyle ayni)

# --- B) SUPURME YAPISI (mesafeler esik_volatilite'nin KATI) ---
SEVIYE_LOOKBACK_DK = 1440         # seviye aramasi: son 24 saat
SEVIYE_KORUMA_DK = 60             # son 60dk'da olusan dip/tepe likidite HAVUZU DEGILDIR
SEVIYE_KUMELEME_VOL = 1.0         # 1 x volatilite icindeki pivotlar ayni seviye
SEVIYE_PIVOT_PENCERE_DK = 15      # pivot tespiti icin +/- pencere

# --- v8.0: SWING SEVIYE HARITASI (scalp'tan AYRI kod yolu) ---
# KURAL: scalp skor yoluna (balina_skoru_hesapla) DOKUNMAZ. _swing_seviye_haritasi
# SAF fonksiyondur; ciktisi yalnizca balina_ayarlar['swing_seviyeler_oto']'ya yazilir.
# Faz 1 esdegerligi (fark=0) korunur.
SWING_SEVIYE_AKTIF   = True        # oto seviye uretimi + yazimi acik (skoru ETKILEMEZ)
# Oncelik: kucuk sayi = yuksek oncelik. Elle > VP > HL > LIQ > SWING_PIVOT > ROUND.
SWING_ONCELIK = {'ELLE': 0, 'VP': 1, 'HL': 2, 'LIQ': 3, 'SWING_PIVOT': 4, 'ROUND': 5}
SWING_ROUND_ADIM       = 1000.0    # yuvarlak-sayi araligi ($). Round sayilar dogasi geregi mutlak.
SWING_ROUND_MENZIL_VOL = 30.0      # anlik fiyatin +/- (bu x vol%) menzilindeki yuvarlaklar
SWING_VP_KOVA_VOL      = 0.25       # VP fiyat kovasi genisligi (vol% kati)
SWING_VP_DEGER_ALANI   = 0.70       # value area orani (POC etrafinda hacmin %70'i — standart VA)
SWING_LIQ_KOVA_VOL     = 0.50       # likidasyon kumesi kova genisligi (vol% kati)
SWING_LIQ_MIN_KAT      = 3.0        # kova hacmi medyan-kovanin bu kati ise "kume" sayilir

# --- v8.1 FAZ B: KADEMELI SWING MOTORU (scalp'tan AYRI kod yolu) ---
# KURAL: scalp skor yoluna (balina_skoru_hesapla) DOKUNULMAZ. Swing kararlari
# _swing_kademe/_swing_hedef_stop SAF fonksiyonlarindan gelir; cikti yalniz
# balina_ayarlar['swing_karar']'a yazilir. Scalp SUSTURMA payload seviyesinde
# (sinyal_durumu) yapilir -> balina_skoru_hesapla degismez, Faz 1 fark=0 korunur.
SWING_MOTOR_AKTIF    = True         # swing motoru uretir + yazar (ayri anahtar)
SCALP_SINYAL_AKTIF   = False        # scalp sinyali SUSTURULDU (kohort/skorlar KAYITTA KALIR;
                                    # yalniz DB'ye yazilan aktif sinyal_durumu susar)
SWING_YAKINLIK_VOL   = 2.0          # fiyat seviyeye bu x vol yaklasinca IZLE kademesi
SWING_MIN_RR         = 2.0          # v8: 1.5 -> 2.0. v9.2: HER IKI yol (grab + kademe)
                                    # rr_kisa'yi bu esikle kapilar (tek tanim — GK-4)
SWING_STOP_TAMPON_VOL = 0.2         # stop = yapisal seviye +/- bu x vol (tampon)
SWING_FUNDING_ASIRI  = 0.0005       # |funding| > bu -> kalabalik asiri (HAZIRLAN tetigi)
SWING_YAPISAL = ('ELLE', 'VP', 'HL', 'SWING_PIVOT', 'LIQ')  # stop bunlardan (ROUND zayif, stop olmaz)
# v8.2 FAZ C: KAYIT (arsiv + swing kohortu). Scalp'tan AYRI; skoru ETKILEMEZ.
SWING_ARSIV_AKTIF = True            # dakikalik swing kolonlarini balina_avcisi_data'ya YAZ
                                   # (UPDATE ile; SQL kolonlari yoksa gracefully atlar).
SWING_UFUKLAR = (('4s', 4*3600), ('12s', 12*3600), ('1g', 86400), ('3g', 3*86400))  # swing geri-test ufuklari

# ================== v8: LIQ GRAB SWING MOTORU (15dk KAPALI mum tabani) ==================
# "likidite avi -> DONUS veya DEVAM" kurulumu; order flow teyidi; 15dk mum kapanisi
# disiplini (karar ASLA mum icinde). Baslangic degerleri "makul"dur, "dogru" degil —
# n>=20 kohort olayi birikince SQL ile kalibre edilecek; o zamana kadar DEGISTIRILMEZ
# (her degisiklik kohortu kirletir). Tum sinyaller KAGIT USTU (gercek emir yok).
SWING_SEVIYE_MIN_GUC   = 40      # v8: bu gucun altindaki seviyede grab motoru CALISMAZ
SWING_COKZAMAN_BANT    = 0.0015  # v8: 1s/4s pivot cakisma bandi (fiyatin ORANI, vol degil)
SWEEP_MIN_DELME_PCT    = 0.0008  # v8: min delme derinligi (fiyat orani) — max(bu, 0.2xATR15)
SWEEP_ESLIK_HACIM_KAT  = 1.5     # v8: sweep mumunun hacmi son 20 mum ort. bu kati olmali
                                 #     (veya 15dk penceresinde likidasyon > 0)
SWEEP_COOLDOWN_DK      = 90      # v8: ayni seviyede ikinci aday uretmeme suresi
SWEEP_GOVDE_ORAN       = 0.25    # v8: DONUS icin |close-seviye| >= bu x mum araligi
                                 #     (kil payi geri kapanis DONUS sayilmaz -> tip None)
SWEEP_STOP_TAMPON_PCT  = 0.0005  # v8: stop = fitil ucu +/- max(bu x fiyat, 0.1xATR15)
SWING_EQ_BANT          = 0.001   # v8 G3: equal highs/lows — ayni tur iki+ 1s/4s pivotu bu
                                 #     orandan yakinsa EQ kumesi (en yogun stop kumeleri)
CHOCH_MAX_MUM          = 16      # v8 G2: sweep'ten sonra bu kadar 15dk mum (4 saat) icinde
                                 #     CHoCH gelmezse sonuc KESINLESIR (kayit amacli; sinyal
                                 #     sarti DEGIL — beklemek gecikme ekler, once kohort olcer)
LIK_BAYATLIK_SN        = 240     # v8: son BASARILI Coinalyze likidasyon cekiminden bu kadar
                                 #     saniye gectiyse dakika izine None yazilir ("olculemedi";
                                 #     0.0 "likidasyon yok" demek DEGILDIR — sifir tuzagi).
                                 #     CVD_BAYATLIK_SN ile ayni gerekce, ayri kaynak.
# v8 ADIM 1 — seviye GUC puani (0-100): cakisan kaynaklarin puanlari TOPLANIR (tavan 100).
# NOT: HL spec tablosunda YOK -> 0 puan (bilinçli; kalibrasyonda tekrar bakilacak).
SWING_GUC_PUAN = {'ELLE': 40, 'COKZAMAN': 25, 'LIQ': 20, 'VP': 15, 'ROUND': 10,
                  'SWING_PIVOT': 5, 'EQ': 20}
GRAB_MUM_SEMBOL = "BTCUSDT_PERP.A"   # v8: 15dk/1s/4s kline kaynagi (Binance perp — anlik
                                     # fiyatla ayni borsa; coklu-borsa high/low karisimi olmaz)
# v8: rejim AILE kumeleri — TEK tanim (GK-4). surec_takip_et'in yerel kopyalari buraya
# tasindi; grab teyidi (_sweep_teyit) de AYNI kumeleri okur. Degerler birebir ayni —
# davranis degismez (Faz 1).
DAGITIM_AILESI = frozenset({"TEPE_DAGITIM", "SHORT_SQUEEZE", "SHORT_TASFIYE",
                            "TASFIYE_SONRASI_DONUS",
                            "TEPE_DAGITIM_SPOT", "TEPE_DAGITIM_TEYITSIZ", "TEPE_DAGITIM_PERP"})
TOPLAMA_AILESI = frozenset({"DIP_TOPLAMA", "LONG_TASFIYE", "LONG_KAPITULASYON",
                            "DIP_TOPLAMA_SPOT", "DIP_TOPLAMA_TEYITSIZ", "DIP_TOPLAMA_PERP"})

SUPURME_YAKINLIK_VOL = 2.0        # fiyat seviyeye 2 x vol yaklasinca "silahlan"
SUPURME_MIN_DELME_VOL = 0.3       # fitil en az 0.3 x vol kadar otesine gecmeli
SUPURME_MAX_DELME_VOL = 8.0       # bundan derin = KIRILMA, supurme degil
SUPURME_GERI_ALIM_MAX_DK = 15     # fitil sonrasi geri alim icin azami sure
SUPURME_GECERLILIK_DK = 30        # geri alimdan sonra kurulum bu kadar "taze"
SUPURME_COOLDOWN_DK = 60          # ayni seviye icin tekrar tetiklenme yasagi

# --- KAPITULASYON (zaten adaptif: esik_c_neg'in kati) ---
KAPITULASYON_CARPANI = 1.5        # d_vadeli <= esik_c_neg * 1.5 (tepe icin simetrik)

# ================== v7.5 — EMİLİMİN YÖNÜ ==================
# FAZ 1: SADECE ÖLÇER. Skoru ve sinyali ETKİLEMEZ.
# v7.8 NOT: v7.5'in EMILIM_YONU_AKTIF bayragi KALDIRILDI — hicbir kod okumuyordu.
# Boyle bir bayrak tam v7.3.1'de ogrenilen NO-OP mayinidir: Faz 2'de True yapilir,
# hicbir sey degismez, sebebi haftalarca aranir. Emilimin Faz-2 salteri ZATEN
# EMILIM_AYRIMI_AKTIF'tir (aile eslemesine bagli); ikinci bir salter kafa karistirir.
# v8.7: buradaki EMILIM_OLCUM_AKTIF cift tanimi KALDIRILDI (asagida v7.4 blogunda
# tek tanim var; ilkini degistirmek sessiz NO-OP idi — denetim bulgusu).

# --- satici tukenmesi ---
TUKENME_DILIM_DK = 15             # ardisik dilim uzunlugu (dakika)
TUKENME_DILIM_SAYISI = 3          # kac dilim karsilastirilir (3 x 15dk = 45dk)
TUKENME_SONME_ORANI = 0.50        # son/ilk < 0.50 -> satis YARIYA indi = TUKENME
TUKENME_MIN_AKIS = 0.50           # ilk dilimde en az bu kadar (adaptif birim) satis
                                  # olmali; yoksa "tukenme" anlamsizdir (sifir tuzagi)

# --- emilim borsasi (SPOT order book) ---
SPOT_OB_MAX_YAS_SN = 180          # spot defter bundan bayatsa metrik None (0.0 DEGIL)
PERP_OB_MAX_YAS_SN = 180          # v7.7: perp defter bayatlik siniri (spot ile simetrik).
                                  # SADECE mutabakat sayimini kapsar; skor yolunu (duvar
                                  # haritasi) FAZ 1'de DEGISTIRMEZ — v7.2 esdegerligi korunur.
EMILIM_EGILIM_ESIGI = 0.15        # (bid-ask)/(bid+ask) >= 0.15 -> o defter BID-AGIR
EMILIM_DERINLIK_PCT = 0.01        # +/-%1 bandi (mevcut derinlik olcusuyle ayni)

# --- KOHORT (olcum) ---
KOHORT_KUME_DK = 30               # ayni yonde <=30dk arayla gelen olaylar ayni kume
KOHORT_AZAMI_KAYIT = 500          # balina_ayarlar JSONB sisirilmesin

# ================== v7.4 — EMİLİM AYRIMI (toplama mi dagitim mi) ==================
# Absorbsiyon YONU: negatif CVD + duz fiyat + kalin bid AYNI grafigi uretir ama
# balina ya EMICI (toplama) ya AGRESIF SATAN (dagitim) olabilir. Bunu ayirmaya
# calisan UC vekil metrik. HICBIRI FAZ 1'de skoru etkilemez.
# BILINEN RISK (spec §9.1): emilim_borsasi spot ORDER BOOK'unu GORMEZ (sistemde
# yok, yeni REST = ban riski) — yalnizca agresif satisin agirlik merkezini olcer.
# Nedensel iddia ("coin toplayan spot alir") MAKUL ama KANITLANMAMIS; kohort
# fark gostermezse metrik ATILIR.
EMILIM_AYRIMI_AKTIF = False       # FAZ 1: KAPALI. Sinyal davranisi BIREBIR ayni.
EMILIM_OLCUM_AKTIF = True         # olcum + kohort kaydi acik (davranis degistirmez)

EMILIM_MIN_AKIS = 0.25            # bunun altinda akis yok -> metrikler None (0.0 DEGIL!)
EMILIM_GUCLU_ESIK = 0.35          # esneklik < 0.35 -> GUCLU emilim
EMILIM_YOK_ESIK = 1.00            # esneklik > 1.00 -> emilim YOK (fiyat serbest)
EMILIM_SPOT_ESIGI = 0.65          # spot_pay >= 0.65 -> satis agirlikli SPOT
# v7.6: TUKENME_DILIM_SAYISI/SONME_ORANI yukarida (v7.5 blogu) TANIMLI — burada
# TEKRAR tanimlamak MUKERRERDI (dogrulama tespiti). TUKENME_MAX_DUSUS_VOL ise
# fiyat-sarti icin gerekli; korunur.
TUKENME_MAX_DUSUS_VOL = 2.0       # fiyat 2 x vol'den fazla ters giderse tukenme DEGIL
# VE-KAPISI minimum eşikleri: herhangi biri altında kalırsa sinyal YOK
# (ortalama/telafi yok — zayıf katman güçlülerle örtülemez)
VE_ISLEM_MIN = 0.45       # işlem yoğunluğu (katman 2) en az bu kadar güçlü olmalı
VE_DIRENC_MIN = 0.40      # fiyat direnci/zayıflığı net olmalı
VE_DUVAR_MIN = 0.30       # duvar teyitli VE anlamlı olmalı
GERI_TEST_UFUK_DK = 15    # sinyalin isabeti kaç dakika sonra ölçülsün


# =========================================================================
# RENDER KALKANI (7/24 uyanık tutucu) - DEĞİŞMEDİ
# =========================================================================
class RenderKalkanHandler(BaseHTTPRequestHandler):
    """
    v5.4 — Render sunucusu artik panelleri de servis ediyor.
    Rotalar:
      /            -> saglik kontrolu (Render'in uyanik tutmasi icin)
      /mobil       -> swing kokpiti MOBIL (v8.4.1: kullanicinin telefonundaki
                      mevcut kisayol bu adrese kayitli — yeni panel BURADAN servis
                      edilir ki telefonda yeniden kurulum gerekmesin)
      /mobil-eski  -> eski telefon paneli (balina_mobil.html — arsiv)
      /panel       -> masaustu paneli (v3balina_sonar_terminal.html)
      /kokpit      -> swing kokpiti (v4balina_swing_kokpit.html)
      /kokpit-mobil-> /mobil ile ayni dosya (v8.4 adresi — yer imleri kirilmasin)
    Panel dosyalari main.py ile AYNI KLASORDE olmali (repo koku).
    Boylece GitHub Pages / ayri repo / dosya transferi gerekmez;
    tek link: https://<servis>.onrender.com/mobil
    """

    PANEL_DOSYALARI = {
        # v8.4.1: /mobil artik YENI mobil kokpiti acar — kullanicinin telefonundaki
        # kisayol bu adrese kayitliydi; panel adres degistirmek yerine adresin
        # icerigi degistirildi. Eski panel SILINMEDI: /mobil-eski'de arsiv.
        "/mobil": "v4balina_swing_kokpit_mobil.html",
        "/mobil-eski": "balina_mobil.html",
        "/panel": "v3balina_sonar_terminal.html",
        "/kokpit": "v4balina_swing_kokpit.html",   # v8.3: rotasiz paneldi -> 404 (denetim)
        "/kokpit-mobil": "v4balina_swing_kokpit_mobil.html",   # v8.4 adresi (yer imi uyumu)
    }

    def _panel_gonder(self, dosya_adi):
        try:
            # Panel dosyasini bul: once main.py'nin klasoru, sonra calisma dizini
            try:
                kok = os.path.dirname(os.path.abspath(__file__))
            except NameError:
                kok = os.getcwd()
            yol = os.path.join(kok, dosya_adi)
            if not os.path.exists(yol):
                yol = os.path.join(os.getcwd(), dosya_adi)
            with open(yol, "rb") as f:
                icerik = f.read()
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, must-revalidate")
            self.send_header("Content-Length", str(len(icerik)))
            self.end_headers()
            self.wfile.write(icerik)
        except FileNotFoundError:
            self.send_response(404)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                f"<h3>Panel bulunamadi: {dosya_adi}</h3>"
                f"<p>Bu dosyayi main.py ile ayni klasore (repo koku) yukleyin.</p>"
                .encode("utf-8")
            )
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"Panel servis hatasi: {e}".encode("utf-8"))

    def do_GET(self):
        yol = self.path.split("?")[0].rstrip("/") or "/"

        if yol in self.PANEL_DOSYALARI:
            self._panel_gonder(self.PANEL_DOSYALARI[yol])
            return

        # Saglik kontrolu (Render uyanik tutucu + hizli durum)
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><head><meta charset='utf-8'>"
            b"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            b"<style>body{background:#03101a;color:#e9f5fb;font-family:system-ui;"
            b"padding:40px;text-align:center}a{color:#2ff5c8;display:block;margin:14px;"
            b"font-size:18px;text-decoration:none;border:1px solid #164257;padding:14px;"
            b"border-radius:10px}</style></head><body>"
            b"<h2>&#128011; Balina Avcisi</h2>"
            b"<p style='color:#6d97ad'>Motor calisiyor 7/24</p>"
            b"<a href='/mobil'>Mobil Kokpit</a>"
            b"<a href='/panel'>Masaustu Panel</a>"
            b"<a href='/kokpit'>Swing Kokpiti</a>"
            b"<a href='/mobil-eski'>Eski Mobil Panel (arsiv)</a>"
            b"</body></html>"
        )

    def log_message(self, format, *args):
        pass

def web_sunucu_calistir():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), RenderKalkanHandler)
    logging.info(f"Render guvenlik kalkani aktif. Port: {port}")
    server.serve_forever()


# =========================================================================
# REST YARDIMCILARI - DEĞİŞMEDİ
# =========================================================================
def rest_yardimci_guncelle():
    base = "https://fapi.binance.com"
    bybit_base = "https://api.bybit.com"
    session = requests.Session()
    while True:
        try:
            fr = session.get(f"{base}/fapi/v1/premiumIndex?symbol=BTCUSDT", timeout=5).json()
            oi = session.get(f"{base}/fapi/v1/openInterest?symbol=BTCUSDT", timeout=5).json()
            depth = session.get(f"{base}/fapi/v1/depth?symbol=BTCUSDT&limit=1000", timeout=8).json()

            with durum.lock:
                if isinstance(fr, dict) and fr.get('lastFundingRate') is not None:
                    durum.funding_rate = float(fr.get('lastFundingRate'))
                if isinstance(oi, dict) and oi.get('openInterest') is not None:
                    durum.open_interest = float(oi.get('openInterest'))
                if isinstance(depth, dict) and 'bids' in depth:
                    yeni_bids = {}
                    for fiyat_s, miktar_s in depth['bids']:
                        m = float(miktar_s)
                        if m > 0:
                            yeni_bids[float(fiyat_s)] = m
                    durum.bids = yeni_bids
                    yeni_asks = {}
                    for fiyat_s, miktar_s in depth.get('asks', []):
                        m = float(miktar_s)
                        if m > 0:
                            yeni_asks[float(fiyat_s)] = m
                    durum.asks = yeni_asks
                    durum.perp_bids_zaman = time.time()   # v7.7: bayatlik damgasi

            time.sleep(0.3)

            # ===== v8.6: BINANCE GLOBAL L/S ORANI (Coinalyze fallback'i) =====
            # Coinalyze long-short-ratio canli ortamda bos/0 donuyor (panel 'veri yok').
            # Binance /futures/data/globalLongShortAccountRatio ucretsiz+public ve
            # kalabalik baglami icin yeterli. Oncelik Coinalyze'da kalir: ozet dongusu
            # yalniz agg_ls<=0 iken bunu kullanir. +1 cagri/dk, dusuk agirlik — ban
            # riski yok. Basarisizsa zaman damgasi guncellenmez -> bayat sayilir
            # (sifir tuzagi: eski deger taze gibi okunmaz).
            try:
                bls = session.get(
                    f"{base}/futures/data/globalLongShortAccountRatio"
                    f"?symbol=BTCUSDT&period=5m&limit=1", timeout=5).json()
                if isinstance(bls, list) and bls:
                    _blsv = float(bls[-1].get('longShortRatio', 0) or 0)
                    if _blsv > 0:
                        with durum.lock:
                            durum.binance_ls_ratio = _blsv
                            durum.binance_ls_zaman = time.time()
            except Exception as e:
                logging.warning(f"Binance L/S hatasi: {e}")

            time.sleep(0.3)

            # ===== v7.5: BINANCE SPOT ORDER BOOK =====
            # api.binance.com = SPOT (fapi = vadeli). Ayri host, ayri limit havuzu.
            # depth?limit=1000 agirligi 50; dakikada 1 kez -> limitin %2'si. Guvenli.
            # Hata durumunda SESSIZCE eski defteri KORUMAZ: spot_ob_zaman guncellenmez,
            # okuyucu bayatligi gorup metrigi None yapar (sifir tuzagina dusmeyiz).
            try:
                sp = session.get(
                    "https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=1000",
                    timeout=8
                ).json()
                if isinstance(sp, dict) and 'bids' in sp and 'asks' in sp:
                    yeni_sb = {}
                    for fiyat_s, miktar_s in sp['bids']:
                        m = float(miktar_s)
                        if m > 0:
                            yeni_sb[float(fiyat_s)] = m
                    yeni_sa = {}
                    for fiyat_s, miktar_s in sp['asks']:
                        m = float(miktar_s)
                        if m > 0:
                            yeni_sa[float(fiyat_s)] = m
                    if yeni_sb and yeni_sa:
                        with durum.lock:
                            durum.spot_bids = yeni_sb
                            durum.spot_asks = yeni_sa
                            durum.spot_ob_zaman = time.time()
            except Exception as e:
                logging.warning(f"Spot derinlik hatasi: {e}")

            time.sleep(0.3)

            # ===== v7.6: BYBIT SPOT ORDER BOOK (2. spot borsa) =====
            # category=spot (linear=vadeli). limit=200 -> agirlik dusuk. Basarisizsa
            # zaman guncellenmez -> okuyucu bayat sayar (sifir tuzagi yok).
            try:
                bysp = session.get(
                    f"{bybit_base}/v5/market/orderbook?category=spot&symbol=BTCUSDT&limit=200",
                    timeout=8
                ).json()
                r = bysp.get('result', {}) if isinstance(bysp, dict) else {}
                if r.get('b') or r.get('a'):
                    yb = {float(f): float(m) for f, m in r.get('b', []) if float(m) > 0}
                    ya = {float(f): float(m) for f, m in r.get('a', []) if float(m) > 0}
                    if yb and ya:
                        with durum.lock:
                            durum.bybit_spot_bids = yb
                            durum.bybit_spot_asks = ya
                            durum.bybit_spot_zaman = time.time()
            except Exception as e:
                logging.warning(f"Bybit spot derinlik hatasi: {e}")

            time.sleep(0.3)

            # ===== v7.6: OKX SPOT ORDER BOOK (3. spot borsa) =====
            # instId=BTC-USDT (SWAP degil, spot). books?sz=200.
            try:
                oksp = session.get(
                    "https://www.okx.com/api/v5/market/books?instId=BTC-USDT&sz=200",
                    timeout=8
                ).json()
                od = oksp.get('data', []) if isinstance(oksp, dict) else []
                if od:
                    kitap = od[0]
                    yb = {float(s[0]): float(s[1]) for s in kitap.get('bids', []) if float(s[1]) > 0}
                    ya = {float(s[0]): float(s[1]) for s in kitap.get('asks', []) if float(s[1]) > 0}
                    if yb and ya:
                        with durum.lock:
                            durum.okx_spot_bids = yb
                            durum.okx_spot_asks = ya
                            durum.okx_spot_zaman = time.time()
            except Exception as e:
                logging.warning(f"OKX spot derinlik hatasi: {e}")

            time.sleep(0.3)

            try:
                by_res = session.get(
                    f"{bybit_base}/v5/market/orderbook?category=linear&symbol=BTCUSDT&limit=200",
                    timeout=8
                ).json()
                by_result = by_res.get('result', {}) if isinstance(by_res, dict) else {}
                if by_result.get('b') or by_result.get('a'):
                    with durum.lock:
                        by_bids = {}
                        for fiyat_s, miktar_s in by_result.get('b', []):
                            m = float(miktar_s)
                            if m > 0:
                                by_bids[float(fiyat_s)] = m
                        durum.bybit_bids = by_bids
                        by_asks = {}
                        for fiyat_s, miktar_s in by_result.get('a', []):
                            m = float(miktar_s)
                            if m > 0:
                                by_asks[float(fiyat_s)] = m
                        durum.bybit_asks = by_asks
                        durum.bybit_perp_zaman = time.time()   # v7.7: bayatlik damgasi
            except Exception as e:
                logging.warning(f"Bybit derinlik hatasi: {e}")

            time.sleep(0.3)

            # D: OKX order book (3. borsa derinligi — spoofing tespitini guclendirir)
            # ÖNEMLİ: BTC-USDT-SWAP'ta 1 kontrat = 0.0001 BTC (100 kontrat = 0.01 BTC).
            # Kontrat degeri (ctVal) borsanin instruments API'sinden BİR KEZ cekilir;
            # OKX ileride degistirse kod kendini duzeltir. Cekilemezse guvenli
            # varsayilan 0.01 kullanilir. v8.7: canli Render logu API'nin 0.01
            # dondugunu KANITLADI ("ctVal API'den alindi: 0.01 BTC/kontrat");
            # eski 0.0001 varsayilani 100x KUCUKTU — API dusseydi OKX derinligi
            # 100x eksik olurdu. (Onceki yorum tersini iddia ediyordu; canli veri kazanir.)
            try:
                if not hasattr(rest_yardimci_guncelle, "_okx_ctval"):
                    rest_yardimci_guncelle._okx_ctval = 0.01  # guvenli varsayilan (canli API degeri)
                    try:
                        inst = session.get(
                            "https://www.okx.com/api/v5/public/instruments"
                            "?instType=SWAP&instId=BTC-USDT-SWAP", timeout=8
                        ).json()
                        idata = inst.get('data', []) if isinstance(inst, dict) else []
                        if idata and idata[0].get('ctVal'):
                            rest_yardimci_guncelle._okx_ctval = float(idata[0]['ctVal'])
                            logging.info(f"OKX kontrat degeri (ctVal) API'den alindi: "
                                         f"{rest_yardimci_guncelle._okx_ctval} BTC/kontrat")
                    except Exception as e:
                        logging.warning(f"OKX ctVal cekilemedi, varsayilan 0.0001: {e}")
                OKX_KONTRAT_BTC = rest_yardimci_guncelle._okx_ctval

                okx_res = session.get(
                    "https://www.okx.com/api/v5/market/books?instId=BTC-USDT-SWAP&sz=200",
                    timeout=8
                ).json()
                okx_data = okx_res.get('data', []) if isinstance(okx_res, dict) else []
                if okx_data:
                    kitap = okx_data[0]
                    # OKX format: [fiyat, miktar(kontrat), likidite_emir_sayisi, emir_sayisi]
                    with durum.lock:
                        okx_bids = {}
                        for satir in kitap.get('bids', []):
                            fiyat = float(satir[0]); kontrat = float(satir[1])
                            btc = kontrat * OKX_KONTRAT_BTC
                            if btc > 0:
                                okx_bids[fiyat] = btc
                        durum.okx_bids = okx_bids
                        okx_asks = {}
                        for satir in kitap.get('asks', []):
                            fiyat = float(satir[0]); kontrat = float(satir[1])
                            btc = kontrat * OKX_KONTRAT_BTC
                            if btc > 0:
                                okx_asks[fiyat] = btc
                        durum.okx_asks = okx_asks
                        durum.okx_perp_zaman = time.time()   # v7.7: bayatlik damgasi
            except Exception as e:
                logging.warning(f"OKX derinlik hatasi: {e}")

        except Exception as e:
            logging.warning(f"REST yardimci guncelleme hatasi: {e}")
        time.sleep(60)


# =========================================================================
# COINALYZE MOTORU - DEĞİŞMEDİ (omurga)
# =========================================================================
def coinalyze_btc_sembolleri_kesfet(session, headers):
    for deneme in range(3):
        try:
            fm_res = session.get(f"{COINALYZE_BASE}/future-markets", headers=headers, timeout=10)
            if fm_res.status_code == 429:
                bekle = int(float(fm_res.headers.get('Retry-After', 15)))
                logging.warning(f"Sembol kesfi rate-limit (429), {bekle}sn bekleyip tekrar (deneme {deneme+1}/3).")
                time.sleep(bekle)
                continue
            if fm_res.status_code != 200:
                logging.warning(f"Sembol kesfi basarisiz: {fm_res.status_code}")
                return []
            fm_data = fm_res.json()
            secilenler = []
            for m in fm_data:
                if not isinstance(m, dict):
                    continue
                if (str(m.get('base_asset', '')) == 'BTC'
                        and m.get('is_perpetual') is True
                        and str(m.get('quote_asset', '')) in ('USDT', 'USD', 'USDC')):
                    secilenler.append(m.get('symbol'))
            return secilenler
        except Exception as e:
            logging.warning(f"Sembol kesfi hatasi (deneme {deneme+1}/3): {e}")
            time.sleep(5)
    return []


def coinalyze_spot_sembolleri_kesfet(session, headers):
    for deneme in range(3):
        try:
            sm_res = session.get(f"{COINALYZE_BASE}/spot-markets", headers=headers, timeout=10)
            if sm_res.status_code == 429:
                time.sleep(int(float(sm_res.headers.get('Retry-After', 15))))
                continue
            if sm_res.status_code != 200:
                return []
            secilenler = []
            for m in sm_res.json():
                if not isinstance(m, dict):
                    continue
                if (str(m.get('base_asset', '')) == 'BTC'
                        and str(m.get('quote_asset', '')) in ('USDT', 'USD', 'USDC')
                        and m.get('has_buy_sell_data') is True):
                    secilenler.append(m.get('symbol'))
            return secilenler
        except Exception:
            time.sleep(5)
    return []


MAJOR_BORSA_KODLARI = ['A', '6', '3', '2', '0', 'C', 'K', 'F']

def _majorleri_oncelikle_sec(semboller, maks=5):
    def oncelik(sembol):
        kod = sembol.split('.')[-1] if '.' in sembol else ''
        return MAJOR_BORSA_KODLARI.index(kod) if kod in MAJOR_BORSA_KODLARI else 999
    sirali = sorted(semboller, key=oncelik)
    return sirali[:maks]


def _ayarlar_oku(anahtar):
    try:
        res = supabase.table("balina_ayarlar").select("*").eq("anahtar", anahtar).limit(1).execute()
        if res.data:
            return res.data[0]
        return None
    except Exception as e:
        logging.warning(f"Ayarlar okuma hatasi ({anahtar}): {e}")
        return None


def _ayarlar_oku_katilim(anahtar):
    """v8.7 — read-modify-write icin okuma: (ok, kayit) doner. ok=False = OKUMA
    HATASI; 'kayit yok' (ok=True, None) ile AYRILIR. RMW yapan cagiran, hatada
    yazmayi ATLAMALIDIR — yoksa gecici bir 503 tum olay gecmisini bos listeyle
    ezer (denetim bulgusu; kullanici benzer bir veri kaybini zaten yasadi)."""
    try:
        res = supabase.table("balina_ayarlar").select("*").eq("anahtar", anahtar).limit(1).execute()
        return True, (res.data[0] if res.data else None)
    except Exception as e:
        logging.warning(f"Ayarlar okuma hatasi ({anahtar}): {e}")
        return False, None


def _ayarlar_yaz(anahtar, deger):
    """v7.3: basari durumunu dondurur — kohort tamponu 'yazim dogrulandi mi'
    bilgisine muhtac (sessiz yutma, onaylanmis supurme olayini kaybettirirdi)."""
    try:
        supabase.table("balina_ayarlar").upsert({
            "anahtar": anahtar,
            "deger": deger,
            "guncellenme_zamani": datetime.datetime.utcnow().isoformat()
        }).execute()
        return True
    except Exception as e:
        logging.warning(f"Ayarlar yazma hatasi ({anahtar}): {e}")
        return False


def coinalyze_sembolleri_getir(session, headers, anahtar, kesif_fn, varsayilan, onbellek_saat=24):
    kayit = _ayarlar_oku(anahtar)
    if kayit:
        try:
            guncelleme = datetime.datetime.fromisoformat(kayit['guncellenme_zamani'].replace('Z', '+00:00'))
            yas_saat = (datetime.datetime.now(datetime.timezone.utc) - guncelleme).total_seconds() / 3600
            if yas_saat < onbellek_saat and kayit.get('deger'):
                logging.info(f"Sembol onbellegi kullanildi ({anahtar}, {yas_saat:.1f}s yasinda).")
                return kayit['deger']
        except Exception:
            pass

    bulunan = kesif_fn(session, headers)
    if bulunan:
        secilen = _majorleri_oncelikle_sec(bulunan, maks=5)
        _ayarlar_yaz(anahtar, secilen)
        logging.info(f"Yeni kesif onbellege yazildi ({anahtar}): {secilen}")
        return secilen

    if kayit and kayit.get('deger'):
        logging.warning(f"Kesif basarisiz, eski onbellek kullaniliyor ({anahtar}).")
        return kayit['deger']
    logging.warning(f"Kesif ve onbellek yok, varsayilana dusuluyor ({anahtar}).")
    return varsayilan


def coinalyze_bv_dogrula(session, headers, semboller):
    """
    E maddesi — CVD formulunun GİRDİSİNİ dogrula.
    Formul (2*bv - v) matematiksel olarak kusursuz; tek risk 'bv'nin
    gercekten taker-buy (agresif alici) hacmi olup olmadigi.
    Bunu Binance'in KENDI verisiyle capraz kontrol ederiz:
      - Binance aggTrades'ten son ~5dk taker-buy oranini hesapla.
      - Coinalyze OHLCV'den ayni pencerede bv/v oranini hesapla.
      - Ikisi yakinsa (fark < %15) -> 'bv' taker-buy'dir, formul GÜVENİLİR.
      - Uzaksa -> UYARI logla (formul girdisi supheli, elle bakilmali).
    Sadece bir kez, baslangicta calisir. Basarisiz olursa sessizce gecer
    (dogrulama sart degil, sadece guven artiricidir).
    """
    try:
        simdi = int(time.time())
        bes_dk_once = simdi - 300

        # 1) Binance'in kendi taker-buy orani (referans dogru kaynak)
        bn = session.get(
            "https://fapi.binance.com/fapi/v1/aggTrades?symbol=BTCUSDT&limit=1000",
            timeout=10
        ).json()
        if not isinstance(bn, list) or len(bn) < 50:
            logging.info("E-dogrulama: Binance aggTrades yetersiz, atlaniyor.")
            return
        alici_hacim = 0.0
        satici_hacim = 0.0
        for t in bn:
            miktar = float(t.get('q', 0) or 0)
            # m=True => alici maker => taker SATICI; m=False => taker ALICI
            if t.get('m') is True:
                satici_hacim += miktar
            else:
                alici_hacim += miktar
        toplam_bn = alici_hacim + satici_hacim
        if toplam_bn <= 0:
            return
        binance_taker_buy_orani = alici_hacim / toplam_bn

        # 2) Coinalyze OHLCV'den bv/v orani (tek sembol yeterli - Binance perp)
        binance_perp = next((s for s in semboller if s.endswith('.A')), semboller[0] if semboller else None)
        if not binance_perp:
            return
        time.sleep(0.4)
        vc = session.get(
            f"{COINALYZE_BASE}/ohlcv-history?symbols={binance_perp}&interval=1min"
            f"&from={bes_dk_once}&to={simdi}",
            headers=headers, timeout=10
        ).json()
        toplam_v = 0.0
        toplam_bv = 0.0
        if isinstance(vc, list):
            for borsa in vc:
                for mum in borsa.get('history', []):
                    toplam_v += float(mum.get('v', 0) or 0)
                    toplam_bv += float(mum.get('bv', 0) or 0)
        if toplam_v <= 0:
            logging.info("E-dogrulama: Coinalyze OHLCV yetersiz, atlaniyor.")
            return
        coinalyze_bv_orani = toplam_bv / toplam_v

        # 3) Karsilastir
        fark = abs(binance_taker_buy_orani - coinalyze_bv_orani)
        if fark < 0.15:
            logging.info(
                f"E-DOGRULAMA ✓ 'bv' taker-buy ile UYUMLU. "
                f"Binance taker-buy: {binance_taker_buy_orani:.3f} | "
                f"Coinalyze bv/v: {coinalyze_bv_orani:.3f} | fark: {fark:.3f} "
                f"-> CVD formulu (2*bv-v) girdisi GÜVENİLİR."
            )
        else:
            logging.warning(
                f"E-DOGRULAMA ⚠ Coinalyze AGREGE 'bv' orani Binance'dan sapiyor. "
                f"Binance: {binance_taker_buy_orani:.3f} | Coinalyze bv/v: {coinalyze_bv_orani:.3f} | "
                f"fark: {fark:.3f} (>0.15). NOT: v5.3'ten itibaren bu KRITIK DEGIL — "
                f"borsa bazli BV-FILTRE devrede, bozuk 'bv' veren borsalar her turda "
                f"otomatik disaniyor (BV-FILTRE loglarina bakin). Sapma buyukse "
                f"suclu borsa orada gorunur."
            )
    except Exception as e:
        logging.info(f"E-dogrulama calistirilamadi (kritik degil): {e}")


# =========================================================================
# v5.3 — BORSA BAZLI 'bv' SAĞLIK FİLTRESİ
# =========================================================================
# E-dogrulama defalarca sapma gosterdi (bv/v = 0.931 gibi imkansiz degerler).
# Sebep: Coinalyze'in BAZI borsalar icin 'bv' alani taker-buy DEGIL (ya toplam
# alim, ya bozuk/eksik veri). Bu, CVD formulunu (2*bv - v) sistematik carpitir:
#   bv/v = 0.93 ise CVD = 2(0.93v) - v = +0.86v  -> neredeyse tum hacim "alim"
#
# Cozum: her borsanin bv/v oranini AYRI kontrol et. Gercek bir piyasada taker-buy
# orani ~0.35-0.65 bandindadir; 0.15-0.85 disi = veri bozuk demektir.
# Bozuk borsa O TURDA DISLANIR; sadece saglikli borsalarin CVD'si toplanir.
# Hangi borsanin bozuk oldugu loglanir -> suclu borsa kesin tespit edilir.
BV_ALT_SINIR = 0.15   # bunun altinda = bozuk (imkansiz derecede satis-agir)
BV_UST_SINIR = 0.85   # bunun ustunde = bozuk (imkansiz derecede alim-agir)

def _borsa_cvd_topla(veri, usd_cevir=False, etiket=""):
    """
    Coinalyze OHLCV cevabindan borsa borsa CVD toplar.
    SAGLIKSIZ borsalari (bv/v orani mantiksiz) DISLAR.
    usd_cevir=True ise BTC cinsinden gelen hacim kapanis fiyatiyla USD'ye cevrilir.
    Donus: (toplam_cvd, saglikli_borsa_sayisi, dislanan_liste)
    """
    toplam = 0.0
    saglikli = 0
    dislanan = []
    for borsa in veri:
        sembol = borsa.get('symbol', '?')
        gecmis = borsa.get('history', [])
        if not gecmis:
            continue
        # Bu borsanin TOPLAM v ve bv'si -> oran saglikli mi?
        b_v = 0.0
        b_bv = 0.0
        for mum in gecmis:
            b_v += float(mum.get('v', 0) or 0)
            b_bv += float(mum.get('bv', 0) or 0)
        if b_v <= 0:
            continue
        oran = b_bv / b_v
        if oran < BV_ALT_SINIR or oran > BV_UST_SINIR:
            dislanan.append(f"{sembol}(bv/v={oran:.2f})")
            continue  # BOZUK -> bu borsayi hesaba KATMA
        # Saglikli borsa: CVD'sini ekle
        for mum in gecmis:
            v = float(mum.get('v', 0) or 0)
            bv = float(mum.get('bv', 0) or 0)
            delta = (2 * bv) - v
            if usd_cevir:
                kapanis = float(mum.get('c', 0) or 0)
                delta = delta * kapanis if kapanis > 0 else delta
            toplam += delta
        saglikli += 1
    if dislanan:
        logging.warning(
            f"BV-FILTRE ({etiket}): {len(dislanan)} borsa DISLANDI (bozuk 'bv') -> "
            f"{', '.join(dislanan)} | {saglikli} saglikli borsa ile devam."
        )
    # v6: KALICI SAYAC — hangi borsa kac kez dislandi (iki hafta sonra analiz icin)
    try:
        with durum.lock:
            durum.bv_toplam_tur += 1
            if dislanan:
                durum.bv_dislanan_tur += 1
                for d in dislanan:
                    sembol_ad = d.split('(')[0]
                    durum.bv_dislanan_sayac[sembol_ad] = durum.bv_dislanan_sayac.get(sembol_ad, 0) + 1
    except Exception:
        pass
    return toplam, saglikli, dislanan


def coinalyze_guncelle():
    if not COINALYZE_API_KEY:
        logging.warning("COINALYZE_API_KEY tanimli degil! Aggregated veri cekilemeyecek.")
        return

    session = requests.Session()
    headers = {"api_key": COINALYZE_API_KEY}
    logging.info("Coinalyze cok-borsali aggregation motoru baslatildi.")

    semboller = coinalyze_sembolleri_getir(
        session, headers, "coinalyze_vadeli_semboller",
        coinalyze_btc_sembolleri_kesfet, ["BTCUSDT_PERP.A", "BTCUSDT.6"]
    )
    spot_semboller = coinalyze_sembolleri_getir(
        session, headers, "coinalyze_spot_semboller",
        coinalyze_spot_sembolleri_kesfet, ["BTCUSDT.A", "BTCUSDT.6"]
    )
    sembol_param = ",".join(semboller)
    spot_param = ",".join(spot_semboller)
    logging.info(f"VADELI SEMBOLLER ({len(semboller)} borsa) -> {sembol_param}")
    logging.info(f"SPOT SEMBOLLER ({len(spot_semboller)} borsa) -> {spot_param}")

    # E: Coinalyze 'bv' alaninin gercekten taker-buy oldugunu bir kez dogrula
    coinalyze_bv_dogrula(session, headers, semboller)

    time.sleep(10)

    while True:
        try:
            simdi = int(time.time())
            bes_dk_once = simdi - 300

            liq_url = (f"{COINALYZE_BASE}/liquidation-history"
                       f"?symbols={sembol_param}&interval=1min"
                       f"&from={bes_dk_once}&to={simdi}&convert_to_usd=true")
            liq_res = session.get(liq_url, headers=headers, timeout=15)
            long_liq = 0.0
            short_liq = 0.0
            lik_damga = None          # v8.8-E: pencere zaman damgasi (son veri noktasi)
            lik_borsa = None          # v8.8-E: history donduren borsa sayisi
            if liq_res.status_code == 200:
                data = liq_res.json()
                if isinstance(data, list):
                    # v8.9-B: ayristirma SAF fonksiyonda (parse birebir ayni; tek fark
                    # sifir tuzagi — 0 borsa "olculemedi"dir, None doner)
                    long_liq, short_liq, lik_damga, lik_borsa = _lik_penceresi_ayristir(data)
                    if lik_borsa is None:
                        logging.info("LIKIDASYON: API liste dondu ama hicbir borsada history yok "
                                     "-> lik_borsa_sayisi=None (olculemedi)")
                    durum.coinalyze_saglikli = True
                    # v8: likidasyon tazelik damgasi — grab dakika izi bayatken None yazar
                    durum.coinalyze_liq_zaman = time.time()
                    # v8.8-E: DONMA tespiti — deger + pencere damgasi ardisik turda
                    # birebir ayniysa sayac artar. AYRI alan; lik_ok/karar zinciri
                    # DEGISMEZ (spec E: mevcut alana None yazmak Faz 1 ihlali olurdu).
                    _lik_simdiki = (long_liq, short_liq, lik_damga)
                    coinalyze_guncelle._lik_donma = _lik_donma_guncelle(
                        getattr(coinalyze_guncelle, '_lik_onceki', None), _lik_simdiki,
                        getattr(coinalyze_guncelle, '_lik_donma', 0))
                    coinalyze_guncelle._lik_onceki = _lik_simdiki
            elif liq_res.status_code == 429:
                bekle = int(float(liq_res.headers.get('Retry-After', 20)))
                logging.warning(f"Coinalyze rate-limit (429), {bekle}sn bekleniyor.")
                time.sleep(bekle)
            else:
                logging.warning(f"Likidasyon hatasi: {liq_res.status_code} | {liq_res.text[:150]}")

            time.sleep(0.4)

            oi_url = f"{COINALYZE_BASE}/open-interest?symbols={sembol_param}&convert_to_usd=true"
            oi_res = session.get(oi_url, headers=headers, timeout=15)
            agg_oi = 0.0
            if oi_res.status_code == 200:
                data = oi_res.json()
                if isinstance(data, list):
                    for borsa in data:
                        agg_oi += float(borsa.get('value', 0) or 0)

            time.sleep(0.4)

            # v9.2 KADANS: funding + L/S 5 turda BIR cekilir (2 cagri/dk -> 0.4).
            # Canli veri (23-25 Tem dump): lik korlugu %48, 133 blok NEREDEYSE TUM
            # saatlere yayilmis (medyan 6dk, max 64dk) -> kronik 429/kredi baskisi.
            # Funding 8 saatte, L/S dakikalar icinde yavas degisir; dakikalik cekim
            # israfti. Skip turunda None = "bu tur olculmedi" (sifir tuzagi, GK):
            # lock blogu None'i YAZMAZ, son gercek olcum korunur. Binance funding
            # (dakikalik, ayri thread) ve Binance L/S fallback zaten devrede.
            coinalyze_guncelle._fr_ls_tur = getattr(coinalyze_guncelle, '_fr_ls_tur', -1) + 1
            agg_fr = None
            agg_ls = None
            if coinalyze_guncelle._fr_ls_tur % 5 == 0:
                fr_url = f"{COINALYZE_BASE}/funding-rate?symbols={sembol_param}"
                fr_res = session.get(fr_url, headers=headers, timeout=15)
                agg_fr = 0.0
                if fr_res.status_code == 200:
                    data = fr_res.json()
                    if isinstance(data, list) and len(data) > 0:
                        degerler = [float(b.get('value', 0) or 0) for b in data]
                        # v8.5 BIRIM DUZELTMESI: Coinalyze funding 'value' YUZDE doner
                        # (0.01 = %0.01), Binance lastFundingRate ise ONDALIK (0.0001 = %0.01).
                        # Kod bunlari ayni sayiyordu -> agg_funding funding_rate'i EZERKEN
                        # 100x sisiyordu (panel %1 gosteriyor, gercekte ~%0.01). Sonuc:
                        # SWING_FUNDING_ASIRI + surec_takip_et funding esikleri SUREKLI
                        # yaniliyordu (sahte "funding asiri" -> sahte HAZIRLAN). /100 ile
                        # Binance ondalik birimine hizalanir. (Funding scalp SKORUNA girmez;
                        # Faz 1 fark=0 etkilenmez.)
                        agg_fr = (sum(degerler) / len(degerler)) / 100.0

                time.sleep(0.4)

                ls_url = (f"{COINALYZE_BASE}/long-short-ratio-history"
                          f"?symbols={sembol_param}&interval=1min"
                          f"&from={bes_dk_once}&to={simdi}")
                ls_res = session.get(ls_url, headers=headers, timeout=15)
                agg_ls = 0.0
                if ls_res.status_code == 200:
                    data = ls_res.json()
                    if isinstance(data, list) and len(data) > 0:
                        son_oranlar = []
                        for borsa in data:
                            hist = borsa.get('history', [])
                            if hist:
                                son_oranlar.append(float(hist[-1].get('r', 0) or 0))
                        if son_oranlar:
                            agg_ls = sum(son_oranlar) / len(son_oranlar)
                if agg_ls <= 0:
                    # v8.6 TANI: L/S neden 0? (canli panel 'veri yok' gosteriyordu, sebep
                    # hic loglanmiyordu). 30 dongude bir logla — spam yapmadan Render
                    # loglarinda kok neden gorunur olsun (status/govde). Binance fallback
                    # devrede oldugundan metrik yine dolar. (v9.2: sayac yalniz CEKIM
                    # turlarinda isler — skip turu "bos" sayilmaz.)
                    coinalyze_guncelle._ls_diag = getattr(coinalyze_guncelle, '_ls_diag', 0) + 1
                    if coinalyze_guncelle._ls_diag % 30 == 1:
                        logging.warning(
                            f"Coinalyze L/S bos (status={ls_res.status_code}, "
                            f"govde[:120]={ls_res.text[:120]!r}) — Binance fallback kullanilacak")

            time.sleep(0.4)

            # VADELİ CVD (formul: 2*bv - v) — v5.3: borsa bazli saglik filtresi
            vadeli_cvd_hesaplandi = False
            yeni_vadeli_cvd = None
            try:
                vc_url = (f"{COINALYZE_BASE}/ohlcv-history"
                          f"?symbols={sembol_param}&interval=5min"
                          f"&from={simdi - 900}&to={simdi}")
                vc_res = session.get(vc_url, headers=headers, timeout=15)
                if vc_res.status_code == 200:
                    veri = vc_res.json()
                    if veri:
                        # Bozuk 'bv' veren borsalar DISLANIR
                        toplam, saglikli, dislanan = _borsa_cvd_topla(
                            veri, usd_cevir=False, etiket="VADELI")
                        # En az 1 saglikli borsa yoksa CVD guvenilmez -> hesaplama
                        if saglikli >= 1:
                            yeni_vadeli_cvd = toplam
                            vadeli_cvd_hesaplandi = True
                        else:
                            logging.warning("VADELI CVD: hicbir borsa saglikli degil, "
                                            "eski deger korunuyor.")
            except Exception as e:
                logging.warning(f"Vadeli CVD hatasi: {e}")

            time.sleep(0.4)

            # SPOT CVD — v5.1: BTC->USD cevrimi + v5.3: borsa saglik filtresi
            spot_cvd_hesaplandi = False
            yeni_spot_cvd = None
            try:
                sc_url = (f"{COINALYZE_BASE}/ohlcv-history"
                          f"?symbols={spot_param}&interval=5min"
                          f"&from={simdi - 900}&to={simdi}")
                sc_res = session.get(sc_url, headers=headers, timeout=15)
                if sc_res.status_code == 200:
                    veri = sc_res.json()
                    if veri:
                        # Bozuk 'bv' veren borsalar DISLANIR + BTC->USD cevrimi
                        toplam, saglikli, dislanan = _borsa_cvd_topla(
                            veri, usd_cevir=True, etiket="SPOT")
                        if saglikli >= 1:
                            yeni_spot_cvd = toplam
                            spot_cvd_hesaplandi = True
                        else:
                            logging.warning("SPOT CVD: hicbir borsa saglikli degil, "
                                            "eski deger korunuyor.")
            except Exception as e:
                logging.warning(f"Spot CVD hatasi: {e}")

            with durum.lock:
                durum.agg_liq_long = long_liq
                durum.agg_liq_short = short_liq
                # v8.8-E: donma teshis alanlari (kayit amacli; kapi DEGIL)
                durum.lik_pencere_damgasi = lik_damga
                durum.lik_borsa_sayisi = lik_borsa
                durum.lik_donma_sayaci = getattr(coinalyze_guncelle, '_lik_donma', 0)
                durum.agg_open_interest = agg_oi
                # v9.2: kadans skip turunda (None) yazilmaz — son gercek olcum korunur
                if agg_fr is not None:
                    durum.agg_funding = agg_fr
                if agg_ls is not None:
                    durum.agg_ls_ratio = agg_ls
                if vadeli_cvd_hesaplandi:
                    durum.agg_vadeli_cvd = yeni_vadeli_cvd
                    durum.coinalyze_cvd_saglikli = True
                    durum.coinalyze_cvd_zaman = time.time()   # FIX3: taze CVD damgasi
                if spot_cvd_hesaplandi:
                    durum.agg_spot_cvd = yeni_spot_cvd
                agg_vadeli_cvd_log = durum.agg_vadeli_cvd
                agg_spot_cvd_log = durum.agg_spot_cvd

            logging.info(
                f"COINALYZE AGG ({len(semboller)}v/{len(spot_semboller)}s borsa) -> "
                f"LongLiq: ${long_liq:,.0f} | ShortLiq: ${short_liq:,.0f} | "
                f"OI: ${agg_oi:,.0f} | "
                f"Funding: {f'{agg_fr:.5f}' if agg_fr is not None else 'atlandi'} | "
                f"L/S: {f'{agg_ls:.2f}' if agg_ls is not None else 'atlandi'} | "
                f"VadeliCVD: {agg_vadeli_cvd_log:,.0f}{'(yeni)' if vadeli_cvd_hesaplandi else '(korunan)'} | "
                f"SpotCVD: {agg_spot_cvd_log:,.0f}{'(yeni)' if spot_cvd_hesaplandi else '(korunan)'}"
            )

            # ============ v8: GRAB KLINE BESLEMESI (15dk + 1s/4s pivot) ============
            # 15dk: her yeni 15dk sinirinda BIR kez (Coinalyze gecikirse sonraki turda
            # yeniden dener — kova ancak kapanan mum GERCEKTEN gelince isaretlenir).
            # 1s/4s: saatte bir (her dakika cekmek rate-limit israfi — spec ADIM 1).
            # Tek sembol (GRAB_MUM_SEMBOL): high/low GEOMETRISI coklu borsadan
            # karistirilmaz; anlik fiyatla ayni borsa (Binance perp).
            try:
                # v9.1.1 (canli log 17 Tem): deploy aninda eski+yeni instance AYNI API
                # anahtarini paylasir; ilk turun 3 ek kline cagrisi 429'u tetikliyordu
                # ve ilk 429'dan sonra kalan kline cagrilari da bosuna atilip limiti
                # daha da zorluyordu. Duzeltme: (a) ILK TUR kline cekilmez (60sn sonra
                # gelir — deploy cakismasi penceresi kapanir), (b) tur icinde ilk
                # 429'da kalan kline cagrilari O TUR icin birakilir. Kovalar
                # isaretlenmedigi icin hicbir veri KAYBOLMAZ, yalniz ertelenir.
                _ilk_tur = getattr(coinalyze_guncelle, '_kline_ilk_tur', True)
                coinalyze_guncelle._kline_ilk_tur = False
                _429 = False
                _kova15 = simdi // 900
                if not _ilk_tur and _kova15 != getattr(coinalyze_guncelle, '_son_15dk_kova', 0):
                    time.sleep(0.4)
                    k_res = session.get(
                        f"{COINALYZE_BASE}/ohlcv-history?symbols={GRAB_MUM_SEMBOL}"
                        f"&interval=15min&from={simdi - 900 * 45}&to={simdi}",
                        headers=headers, timeout=15)
                    if k_res.status_code == 200:
                        _veri = k_res.json()
                        _hist = _veri[0].get('history', []) \
                            if (isinstance(_veri, list) and _veri) else []
                        _kapali = _kline_kapali(_hist, simdi, 900)
                        if _kapali:
                            with durum.lock:
                                durum.mumlar_15dk = _kapali
                            if _kapali[-1]['t'] >= (_kova15 - 1) * 900:
                                coinalyze_guncelle._son_15dk_kova = _kova15
                    else:
                        _429 = k_res.status_code == 429
                        logging.warning(f"GRAB 15dk kline hatasi: {k_res.status_code} "
                                        f"| {k_res.text[:120]}"
                                        + (" — kalan kline cagrilari bu tur atlanacak" if _429 else ""))
                _kova_s = simdi // 3600
                if (not _ilk_tur and not _429
                        and _kova_s != getattr(coinalyze_guncelle, '_son_saat_kova', 0)):
                    _pv = {}
                    for _iv, _per, _ad in (('1hour', 3600, 'pivotlar_1s'),
                                           ('4hour', 14400, 'pivotlar_4s')):
                        if _429:
                            break         # limit zaten asildi — kalanini sonraki tura birak
                        time.sleep(0.4)
                        p_res = session.get(
                            f"{COINALYZE_BASE}/ohlcv-history?symbols={GRAB_MUM_SEMBOL}"
                            f"&interval={_iv}&from={simdi - _per * 210}&to={simdi}",
                            headers=headers, timeout=15)
                        if p_res.status_code == 200:
                            _veri = p_res.json()
                            _hist = _veri[0].get('history', []) \
                                if (isinstance(_veri, list) and _veri) else []
                            _pv[_ad] = _kline_pivotlar(_kline_kapali(_hist, simdi, _per))
                        else:
                            _429 = p_res.status_code == 429
                            # sessiz kalirsa eksik COKZAMAN/EQ loglardan taniz edilemez
                            logging.warning(f"GRAB {_iv} kline hatasi: {p_res.status_code} "
                                            f"| {p_res.text[:120]}")
                    if _pv:
                        with durum.lock:
                            if 'pivotlar_1s' in _pv:
                                durum.pivotlar_1s = _pv['pivotlar_1s']
                            if 'pivotlar_4s' in _pv:
                                durum.pivotlar_4s = _pv['pivotlar_4s']
                        # iki interval de gelmisse kovayi isaretle; kismi geldiyse
                        # sonraki turda tamamlanir
                        if len(_pv) == 2:
                            coinalyze_guncelle._son_saat_kova = _kova_s
                        logging.info(f"GRAB KLINE: 1s pivot {len(durum.pivotlar_1s)} | "
                                     f"4s pivot {len(durum.pivotlar_4s)} | "
                                     f"15dk mum {len(durum.mumlar_15dk)}")
            except Exception as e:
                logging.warning(f"v8 kline cekme hatasi (akis devam eder): {e}")

        except Exception as e:
            logging.warning(f"Coinalyze guncelleme hatasi: {e}")

        time.sleep(60)


# =========================================================================
# WEBSOCKET İŞLEYİCİLERİ - DEĞİŞMEDİ (omurga)
# =========================================================================
_ws_mesaj_sayaci = 0

def on_message(ws, message):
    try:
        data = json.loads(message)
        if 'stream' not in data:
            if 'result' in data:
                logging.info(f"SUBSCRIBE onaylandi: {data}")
            return
        stream = data.get('stream', '')
        payload = data.get('data', {})
        simdi_ms = int(time.time() * 1000)
        global _ws_mesaj_sayaci
        _ws_mesaj_sayaci += 1

        if 'aggTrade' in stream:
            fiyat = float(payload['p'])
            miktar = float(payload['q'])
            is_buyer_maker = payload['m']
            signed = -miktar if is_buyer_maker else miktar
            with durum.lock:
                durum.anlik_fiyat = fiyat
                durum.trade_gecmisi.append((simdi_ms, signed))
                sinir = simdi_ms - 15 * 60 * 1000
                while durum.trade_gecmisi and durum.trade_gecmisi[0][0] < sinir:
                    durum.trade_gecmisi.popleft()
                # v7.3: gercek fitil takibi — dakikalik ornekleme 1-dk'dan kisa
                # fitili kacirir; tick fiyatlari tick_min/tick_max icin saklanir.
                durum.tick_fiyat_gecmisi.append((simdi_ms, fiyat))
                while durum.tick_fiyat_gecmisi and durum.tick_fiyat_gecmisi[0][0] < sinir:
                    durum.tick_fiyat_gecmisi.popleft()

        elif 'bookTicker' in stream:
            # FIX5: bookTicker SADECE fiyat icin. Eskiden buradaki tick-rule proxy'si
            # (best-bid/ask GORUNEN miktarlari) aggTrade'in GERCEK islem hacmiyle AYNI
            # deque'e (trade_gecmisi) yaziliyor, her tick'te yonu CIFT sayiyordu.
            # aggTrade zaten fiyat + gercek isaretli hacim veriyor; WS-yedek CVD artik
            # saf aggTrade. (trade_gecmisi yalnizca WS-yedek CVD icin okunur ve veri
            # kalite kapisi bu yedegi zaten reddeder — sinyale gitmez.)
            best_bid = float(payload.get('b', 0))
            best_ask = float(payload.get('a', 0))
            if best_bid > 0 and best_ask > 0:
                orta_fiyat = (best_bid + best_ask) / 2
                with durum.lock:
                    durum.anlik_fiyat = orta_fiyat

        durum.son_guncelleme = time.time()
    except Exception as e:
        logging.warning(f"WS mesaj isleme hatasi: {e}")


def on_error(ws, error):
    logging.error(f"WebSocket hatasi: {error}")

def on_close(ws, close_status_code, close_msg):
    logging.warning(f"WebSocket kapandi (kod: {close_status_code}). Yeniden baglanilacak...")

def on_open(ws):
    sub_msg = {
        "method": "SUBSCRIBE",
        "params": [f"{SYMBOL}@aggTrade", f"{SYMBOL}@bookTicker"],
        "id": 1
    }
    ws.send(json.dumps(sub_msg))
    logging.info("Ana WebSocket ACIK. SUBSCRIBE gonderildi (aggTrade, bookTicker).")


def websocket_calistir():
    url = "wss://fstream.binance.com/stream"
    while True:
        try:
            ws = websocket.WebSocketApp(
                url, on_message=on_message, on_error=on_error,
                on_close=on_close, on_open=on_open
            )
            ws.run_forever(ping_interval=180, ping_timeout=60)
        except Exception as e:
            logging.error(f"Ana WebSocket run_forever hatasi: {e}")
        logging.info("5 saniye sonra ana WebSocket yeniden baglanacak...")
        time.sleep(5)


def on_liq_message(ws, message):
    try:
        data = json.loads(message)
        if isinstance(data, dict) and 'result' in data and 'o' not in str(data):
            return
        payload = data['data'] if 'data' in data else data
        o = payload.get('o', {})
        if not o:
            return
        sembol = o.get('s', '')
        if sembol != 'BTCUSDT':
            return
        fiyat = float(o.get('p', 0) or o.get('ap', 0) or 0)
        miktar = float(o.get('q', 0) or 0)
        usdt = fiyat * miktar
        if usdt <= 0:
            return
        # v7.3: YON — forceOrder'da S="SELL" ise bir LONG zorla satiliyor (dip
        # tasfiyesi), S="BUY" ise bir SHORT zorla aliniyor (tepe tasfiyesi).
        # Deque salt-yazilirdi; uclu tuple'a gecis hicbir okuyucuyu bozmaz.
        yon = 'LONG' if str(o.get('S', '')).upper() == 'SELL' else 'SHORT'
        simdi_ms = int(time.time() * 1000)
        with durum.lock:
            durum.likidasyonlar.append((simdi_ms, usdt, yon))
            sinir = simdi_ms - 5 * 60 * 1000
            while durum.likidasyonlar and durum.likidasyonlar[0][0] < sinir:
                durum.likidasyonlar.popleft()
    except Exception as e:
        logging.warning(f"Likidasyon mesaj isleme hatasi: {e}")


def on_liq_open(ws):
    logging.info("Likidasyon WebSocket ACIK (!forceOrder@arr).")


def likidasyon_websocket_calistir():
    url = "wss://fstream.binance.com/ws/!forceOrder@arr"
    while True:
        try:
            ws = websocket.WebSocketApp(
                url, on_message=on_liq_message,
                on_error=lambda w, e: logging.error(f"Likidasyon WS hatasi: {e}"),
                on_close=lambda w, c, m: logging.warning("Likidasyon WS kapandi..."),
                on_open=on_liq_open
            )
            ws.run_forever(ping_interval=180, ping_timeout=60)
        except Exception as e:
            logging.error(f"Likidasyon WS run_forever hatasi: {e}")
        time.sleep(5)


def on_spot_message(ws, message):
    try:
        data = json.loads(message)
        if isinstance(data, dict) and 'result' in data:
            return
        payload = data.get('data', data)
        best_bid = float(payload.get('b', 0) or 0)
        best_ask = float(payload.get('a', 0) or 0)
        best_bid_qty = float(payload.get('B', 0) or 0)
        best_ask_qty = float(payload.get('A', 0) or 0)
        if best_bid <= 0 or best_ask <= 0:
            return
        orta = (best_bid + best_ask) / 2
        simdi_ms = int(time.time() * 1000)
        with durum.lock:
            if durum.spot_son_tick_fiyat > 0:
                if orta > durum.spot_son_tick_fiyat:
                    signed = best_ask_qty
                elif orta < durum.spot_son_tick_fiyat:
                    signed = -best_bid_qty
                else:
                    signed = 0
                if signed != 0:
                    durum.spot_trade_gecmisi.append((simdi_ms, signed))
                    sinir = simdi_ms - 15 * 60 * 1000
                    while durum.spot_trade_gecmisi and durum.spot_trade_gecmisi[0][0] < sinir:
                        durum.spot_trade_gecmisi.popleft()
            durum.spot_son_tick_fiyat = orta
    except Exception as e:
        logging.warning(f"Spot mesaj isleme hatasi: {e}")


def on_spot_open(ws):
    sub_msg = {"method": "SUBSCRIBE", "params": ["btcusdt@bookTicker"], "id": 3}
    ws.send(json.dumps(sub_msg))
    logging.info("Spot WebSocket ACIK. Spot bookTicker aboneligi gonderildi.")


def spot_websocket_calistir():
    url = "wss://stream.binance.com:9443/stream"
    while True:
        try:
            ws = websocket.WebSocketApp(
                url, on_message=on_spot_message,
                on_error=lambda w, e: logging.error(f"Spot WS hatasi: {e}"),
                on_close=lambda w, c, m: logging.warning("Spot WS kapandi..."),
                on_open=on_spot_open
            )
            ws.run_forever(ping_interval=180, ping_timeout=60)
        except Exception as e:
            logging.error(f"Spot WS run_forever hatasi: {e}")
        time.sleep(5)


# =========================================================================
# VADE SONU (EXPIRY) - DEĞİŞMEDİ
# =========================================================================
def ceyreklik_expiry_yakin_mi(su_an, esik_saat=48):
    yil = su_an.year
    ceyrek_aylar = [3, 6, 9, 12]
    for ay in ceyrek_aylar:
        son_gun = calendar.monthrange(yil, ay)[1]
        for gun in range(son_gun, 0, -1):
            tarih = datetime.datetime(yil, ay, gun, 8, 0)
            if tarih.weekday() == 4:
                fark_saat = abs((tarih - su_an).total_seconds()) / 3600
                if fark_saat <= esik_saat:
                    return True
                break
    return False


# =========================================================================
# ADAPTİF EŞİK MOTORU - DEĞİŞMEDİ (omurga)
# =========================================================================
def _yuzdelik(liste, q):
    if not liste:
        return None
    s = sorted(liste)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    alt = int(pos)
    kesir = pos - alt
    if alt + 1 < len(s):
        return s[alt] + kesir * (s[alt + 1] - s[alt])
    return s[alt]


def _aykiri_degerleri_temizle(liste):
    if len(liste) < 10:
        return liste
    q1 = _yuzdelik(liste, 0.25)
    q3 = _yuzdelik(liste, 0.75)
    if q1 is None or q3 is None:
        return liste
    iqr = q3 - q1
    if iqr == 0:
        return liste
    alt_sinir = q1 - 1.5 * iqr
    ust_sinir = q3 + 1.5 * iqr
    temiz = [x for x in liste if alt_sinir <= x <= ust_sinir]
    return temiz if temiz else liste


def _cvd_delta_serisi(zaman_cvd, pencere_sn):
    """
    FIX1 — CVD SEVİYE serisinden 5-dk DEĞİŞİM (delta) serisi üretir; canlı koddaki
    (ozet_ve_analiz_dongusu) pencere mantığının AYNISI: her örnek için ~pencere_sn
    önce en yakın ÖNCEKİ örneği bul, farkı al. Böylece eşik, skorda normalize edilen
    büyüklükle (d_vadeli) AYNI istatistiksel nesne olur. (Gerçek veride ölçek farkı
    ~1.25x çıktı — küçük ama yanlış; bu düzeltme doğru nesneyi ölçer. NOT: sistemin
    sessizliğinin ASIL sebebi bu DEĞİL; skor gerçek veride 90-100'e ulaşıyor ama
    VE-kapıları/süreç/maliyet bloklıyor — bkz. balina_ayarlar['ve_kapisi_redleri'].)

    Veri boşluklarını ele: gerçek aralık [150sn, ~2.5x pencere] dışındaysa o eşleşme
    ATILIR (delik üzerinden eşleşme sahte-büyük delta üretir, eşiği tekrar şişirir).
    Girdi: [(epoch, cvd_seviye), ...] (sırasız olabilir). Çıktı: [delta, ...].
    """
    seri = sorted(zaman_cvd, key=lambda x: x[0])
    n = len(seri)
    deltalar = []
    j = 0
    ust_sinir = pencere_sn * 2.5
    for i in range(n):
        ti, ci = seri[i]
        hedef = ti - pencere_sn
        # iki-isaretci: j'yi hedef'ten kucuk/esit son ornege ilerlet (monoton)
        while j + 1 < n and seri[j + 1][0] <= hedef:
            j += 1
        tj, cj = seri[j]
        gap = ti - tj
        if tj < ti and 150 <= gap <= ust_sinir:
            deltalar.append(ci - cj)
    return deltalar


def _fiyat_pct_serisi(zaman_fiyat, pencere_sn):
    """v7.3 — fiyat SEVIYE serisinden 5-dk yuzde-degisim serisi. _cvd_delta_serisi
    ile ayni eslestirme ([150, 2.5x] boslugu korumali); cikti: [|%degisim|, ...].
    esik_volatilite bu dagilimin MEDYANIDIR — tum yapisal esikler bunun katsayisi."""
    seri = sorted(zaman_fiyat, key=lambda x: x[0])
    n = len(seri)
    out = []
    j = 0
    ust_sinir = pencere_sn * 2.5
    for i in range(n):
        ti, fi = seri[i]
        hedef = ti - pencere_sn
        while j + 1 < n and seri[j + 1][0] <= hedef:
            j += 1
        tj, fj = seri[j]
        gap = ti - tj
        if tj < ti and 150 <= gap <= ust_sinir and fj > 0 and fi > 0:
            out.append(abs(fi / fj - 1.0) * 100.0)
    return out


def _emilim_esnekligi(d_fiyat_pct, d_vadeli, d_spot, esik_c, esik_s, esik_vol):
    """
    v7.4 — Fiyat, agresif akisa NE KADAR duyarli? Birim satis basina fiyat hareketi.
    Guclu emici fiyati satisa DUYARSIZ kilar -> esneklik DUSER. Emici yoksa ayni
    satis fiyati ucurur -> esneklik YUKSELIR.
    Olcek-bagimsiz: pay = fiyat%/volatilite-birimi, payda = akis/kendi esikleri.
    Donus: 0'a yakin = GUCLU EMILIM, 1+ = emilim YOK. Akis yoksa None (0 DEGIL!):
    sifir donerse "sakin piyasa" = "mukemmel emilim" sanilir (en kolay kendini
    kandirma yolu — spec §9.2)."""
    akis = abs(d_vadeli) / max(abs(esik_c), 1.0) + abs(d_spot) / max(abs(esik_s), 1.0)
    if akis < EMILIM_MIN_AKIS:
        return None
    hareket = abs(d_fiyat_pct) / max(esik_vol, 0.02)
    return hareket / akis


def _emilim_borsasi(d_vadeli, d_spot, esik_c, esik_s,
                    spot_bid_d=None, spot_ask_d=None,
                    perp_bid_d=None, perp_ask_d=None, spot_ob_yasi_sn=None):
    """
    v7.5 — ARTIK VEKİL DEĞİL: GERÇEK SPOT ORDER BOOK okunur.

    v7.4'te bu metrik spot defteri GOREMIYORDU (sistemde yoktu) ve yalnizca agresif
    satisin AGIRLIK MERKEZINI olcuyordu — spec §9.1'de "KANITLANMAMIS VEKIL" diye
    isaretlenmisti. v7.5'te Binance SPOT depth eklendi (+1 REST/dk, agirlik 50,
    limitin %2'si -> ban riski yok) ve asil soru artik DOGRUDAN sorulabiliyor:

        "Emen taraf HANGI DEFTERDE duruyor?"

    NEDENSEL IDDIA: Coin BIRIKTIREN balina SPOT alir. Perp'te bid koymak envanter
    biriktirmek degil, KALDIRACLI BAHIS oynamaktir. O halde:
      SPOT bid-agir       -> gercek envanter alimi   -> TOPLAMA ihtimali ARTAR
      Yalniz PERP bid-agir-> kaldiracli destek       -> KIRILGAN, envanter YOK
      Ikisi de ask-agir   -> emici yok               -> dagitim

    Donus: (borsa, spot_egilim, perp_egilim, spot_pay)
      borsa: 'SPOT'|'PERP'|'HER_IKISI'|'YOK'  (defter varsa)
             'SPOT_AKIS'|'VADELI_AKIS'|'KARISIK'  (defter BAYAT -> v7.4 vekiline dus)
             None (hicbir sey olculemiyor)
      egilim = (bid-ask)/(bid+ask): +1 tam bid-agir, -1 tam ask-agir. None olabilir.
    ASLA 0.0 dondurmez (spec §9.2 sifir tuzagi): olculemiyorsa None.
    """
    # --- akis tabanli VEKIL (v7.4) — spot defter bayatsa yedek olarak kalir ---
    sv = abs(d_vadeli) / max(abs(esik_c), 1.0)
    ss = abs(d_spot) / max(abs(esik_s), 1.0)
    spot_pay = (ss / (sv + ss)) if (sv + ss) >= EMILIM_MIN_AKIS else None

    perp_top = (perp_bid_d or 0) + (perp_ask_d or 0)
    perp_eg = ((perp_bid_d - perp_ask_d) / perp_top) if perp_top > 0 else None

    # --- GERCEK OLCUM: spot defteri taze mi? ---
    defter_taze = (spot_ob_yasi_sn is not None
                   and spot_ob_yasi_sn <= SPOT_OB_MAX_YAS_SN
                   and (spot_bid_d or 0) + (spot_ask_d or 0) > 0)
    if not defter_taze:
        # Spot defter yok/bayat -> v7.4 VEKILINE dus, ama bunu ADIYLA soyle
        # ('_AKIS' son eki: bu deger DEFTERDEN degil, AKISTAN turedi).
        if spot_pay is None:
            return None, None, perp_eg, None
        if spot_pay >= EMILIM_SPOT_ESIGI:
            return 'SPOT_AKIS', None, perp_eg, round(spot_pay, 4)
        if spot_pay <= 1 - EMILIM_SPOT_ESIGI:
            return 'VADELI_AKIS', None, perp_eg, round(spot_pay, 4)
        return 'KARISIK', None, perp_eg, round(spot_pay, 4)

    spot_eg = (spot_bid_d - spot_ask_d) / ((spot_bid_d or 0) + (spot_ask_d or 0))
    s_bid = spot_eg >= EMILIM_EGILIM_ESIGI
    p_bid = (perp_eg is not None) and (perp_eg >= EMILIM_EGILIM_ESIGI)
    if s_bid and p_bid:
        borsa = 'HER_IKISI'   # en guclu toplama adayi: hem envanter hem destek
    elif s_bid:
        borsa = 'SPOT'        # gercek coin alimi
    elif p_bid:
        borsa = 'PERP'        # kaldiracli destek — envanter YOK, kirilgan
    else:
        borsa = 'YOK'         # iki defter de ask-agir -> emici yok
    return (borsa, round(spot_eg, 4),
            (round(perp_eg, 4) if perp_eg is not None else None),
            (round(spot_pay, 4) if spot_pay is not None else None))


def _akis_tukenmesi(seri, yon, esik_c, esik_s, vol_pct):
    """
    v7.6 — YÖNLÜ akış tükenmesi (satıcı VE alıcı için simetrik).
      yon='SATIS': agresif SATIŞ hızı sönüyor mu? Toplayan balina arzı çeker,
        satıcı havuzu küçülür -> TOPLAMA imzası (DIP_TOPLAMA).
      yon='ALIS' : agresif ALIŞ hızı sönüyor mu? Balina alıcıların üzerine
        dağıtırken alıcı havuzu KURUYUNCA tepe biter -> DAĞITIM imzası (TEPE_DAGITIM).

    v7.5 NIYETI TAMAMLANDI: v7.5 TUKENME_DILIM_DK(15)/TUKENME_MIN_AKIS sabitlerini
    ekledi ama fonksiyona BAGLAMAMISTI (5dk kullaniyordu, sifir korumasi yoktu).
    Artik dilim = TUKENME_DILIM_DK(15dk) x TUKENME_DILIM_SAYISI(3) = 45dk;
    ilk dilimde en az TUKENME_MIN_AKIS akis sarti (sifir tuzagi, spec §9.2).

    YONLU: her dilimde SADECE ilgili yondeki CVD hareketi sayilir (SATIS=negatif,
    ALIS=pozitif). Boylece "satis soniyor" ile "alis basliyor" BIRBIRINE KARISMAZ
    (v7.4'un abs()'i bunlari ayirt edemiyordu).
    Donus: (tukenme_var: bool, sonme_orani: float|None). SESSIZ 0.0 DONDURME."""
    if not seri or len(seri) < 2 or not vol_pct or vol_pct <= 0 or yon not in ('SATIS', 'ALIS'):
        return False, None
    w = TUKENME_DILIM_DK * 60
    simdi = seri[-1]['ts']
    dilim_akis = []
    for k in range(TUKENME_DILIM_SAYISI):
        ust = simdi - k * w
        alt = ust - w
        # YARI-ACIK (alt, ust]: bitisik dilimler sinirdaki kaydi PAYLASMASIN.
        icindekiler = [r for r in seri if alt < r['ts'] <= ust]
        if len(icindekiler) < 2:
            return False, None
        bas, son = icindekiler[0], icindekiler[-1]
        dv = (son.get('vadeli_cvd') or 0) - (bas.get('vadeli_cvd') or 0)
        ds = (son.get('spot_cvd') or 0) - (bas.get('spot_cvd') or 0)
        if yon == 'SATIS':   # negatif CVD hareketi = agresif satis
            akis = max(0.0, -dv) / max(abs(esik_c), 1.0) + max(0.0, -ds) / max(abs(esik_s), 1.0)
        else:                # pozitif CVD hareketi = agresif alis
            akis = max(0.0, dv) / max(abs(esik_c), 1.0) + max(0.0, ds) / max(abs(esik_s), 1.0)
        dilim_akis.append(akis)
    ilk, sonn = dilim_akis[-1], dilim_akis[0]   # ilk=EN ESKI, sonn=EN YENI
    if ilk < TUKENME_MIN_AKIS:                   # SIFIR TUZAGI: baslangicta akis yoksa anlamsiz
        return False, None
    sonme_orani = sonn / ilk
    # Fiyat sarti (YONLU): SATIS tukenmesi (toplama) fiyat COKMEMELI; ALIS
    # tukenmesi (dagitim) fiyat FIRLAMAMALI (firliyorsa alici tukenmiyor, kazaniyor).
    eski_f = yeni_f = None
    tw_alt = simdi - TUKENME_DILIM_SAYISI * w
    for r in seri:
        if r['ts'] >= tw_alt and r.get('fiyat', 0) > 0:
            if eski_f is None:
                eski_f = r['fiyat']
            yeni_f = r['fiyat']
    d_fiyat_tw = ((yeni_f / eski_f - 1.0) * 100.0) if (eski_f and yeni_f) else 0.0
    if yon == 'SATIS':
        fiyat_ok = d_fiyat_tw > -TUKENME_MAX_DUSUS_VOL * vol_pct
    else:
        fiyat_ok = d_fiyat_tw < TUKENME_MAX_DUSUS_VOL * vol_pct
    return ((sonme_orani < TUKENME_SONME_ORANI) and fiyat_ok), sonme_orani
# v7.8: _satici_tukenmesi sarmalayicisi KALDIRILDI — uretimde hicbir yer cagirmiyordu
# (ana dongu dogrudan _akis_tukenmesi(seri,'SATIS',...) kullanir) ve imzasindaki
# pencere_dk parametresi YOK SAYILIYORDU (yaniltici). Testler de gercek giris
# noktasini (_akis_tukenmesi) cagirir.


def _cvd_kaynagi_tutarli(seri, pencere_sn):
    """
    v7.8 — Son pencere_sn icindeki kayitlar TEK CVD kaynagindan mi geldi?

    calculated_cvd iki kaynaktan gelebilir: Coinalyze agregasi ('AGG') veya
    WS-yedek ('WS', yalniz Binance). Ikisinin TABAN SEVIYESI farklidir; kaynak
    gecisinin ustunden delta almak OLCUM degil GURULTUDUR (FIX1 ile ayni sinif:
    outage/restart sonrasi dev sahte delta). Ozellikle Coinalyze DUZELDIKTEN
    sonraki ilk dakikalarda kalite kapisi tekrar 'guvenilir' der ama pencerenin
    eski ucu hala WS-tabanlidir -> karisik-tabanli delta skora sizabilirdi.

    Karisiksa False -> arayan o dakika olcumu ATLAR (None yazar), 0.0 uydurmaz
    (sifir tuzagi §9.2). Bos seri de False (olculemez)."""
    if not seri:
        return False
    alt = seri[-1]['ts'] - pencere_sn
    kaynaklar = {r.get('cvd_kaynak', 'BILINMIYOR') for r in seri if r['ts'] >= alt}
    return len(kaynaklar) <= 1


def _likidite_seviyeleri_bul(zaman_fiyat, vol_pct, tepe_mi=False):
    """
    v7.3 — LIKIDITE HAVUZU tespiti: stop'larin biriktigi ESKI swing dip/tepe.

    Neden basit rolling-min degil: 2 dk once olusan dip likidite havuzu DEGILDIR —
    kimsenin stop'u orada birikmedi. Havuz olmasi icin seviye (a) yeterince eski
    (SEVIYE_KORUMA_DK disari), (b) pivot (yerel ekstrem) olmali.

    zaman_fiyat: [(epoch, fiyat), ...] (sirasiz olabilir; icerde siralanir)
    vol_pct: esik_volatilite (%). 0/None -> bos liste (tespit yapilmaz).
    Donus: [{'fiyat','test','yas_dk'}, ...] — test = kumede kac pivot birlesti.
    """
    if not vol_pct or vol_pct <= 0:
        return []
    simdi = time.time()
    lookback = simdi - SEVIYE_LOOKBACK_DK * 60
    koruma = simdi - SEVIYE_KORUMA_DK * 60
    seri = sorted(((t, f) for t, f in zaman_fiyat
                   if f and f > 0 and lookback <= t <= koruma),
                  key=lambda x: x[0])
    if len(seri) < 60:
        return []
    pen = SEVIYE_PIVOT_PENCERE_DK * 60
    # Kapsama esigi: ±pencerede ~60sn kadansla beklenen ornek sayisinin %80'i.
    # Ic bosluk kontrolu tek basina YETMEZ: pencere SINIRINI asan bir kesinti
    # (orn. dususe girip 40dk sonra yukarida acilan bot) ic-aralik birakmaz ama
    # kesinti sirasindaki fiyat bilinmez -> son ornek sahte "dip seviye" olurdu.
    min_kapsama = max(5, int((2 * pen / 60) * 0.8))
    pivotlar = []
    for i, (ti, fi) in enumerate(seri):
        # +/- pencere icindeki komsular; veri boslugu (>5dk ardisik aralik) pivot
        # penceresini gecersiz kilar (kesinti sirasinda "yerel ekstrem" yanilticidir)
        komsu = []
        bosluk = False
        onceki_t = None
        for tj, fj in seri:
            if tj > ti + pen:
                break              # seri zaman-sirali: pencere sonrasini tarama
            if abs(tj - ti) <= pen:
                if onceki_t is not None and (tj - onceki_t) > 300:
                    bosluk = True
                    break
                komsu.append(fj)
                onceki_t = tj
        if bosluk or len(komsu) < min_kapsama:
            continue
        ekstrem = max(komsu) if tepe_mi else min(komsu)
        if fi == ekstrem:
            pivotlar.append((ti, fi))
    if not pivotlar:
        return []
    # Kumeleme: SEVIYE_KUMELEME_VOL x vol icindeki pivotlar tek seviye (medyan).
    pivotlar.sort(key=lambda x: x[1])
    kumeler = []
    aktif = [pivotlar[0]]
    for p in pivotlar[1:]:
        merkez = aktif[0][1]
        if merkez > 0 and abs(p[1] - merkez) / merkez * 100 <= SEVIYE_KUMELEME_VOL * vol_pct:
            aktif.append(p)
        else:
            kumeler.append(aktif)
            aktif = [p]
    kumeler.append(aktif)
    out = []
    for k in kumeler:
        fiyatlar = sorted(f for _, f in k)
        medyan_f = fiyatlar[len(fiyatlar) // 2]
        en_eski = min(t for t, _ in k)
        out.append({'fiyat': round(medyan_f, 1), 'test': len(k),
                    'yas_dk': round((simdi - en_eski) / 60.0, 0)})
    return out


def _swing_seviye_haritasi(anlik_fiyat, fiyat_seri, vol_pct, dipler, tepeler,
                           liq_kayitlari, elle_seviyeler=None,
                           pivotlar_1s=None, pivotlar_4s=None):
    """
    v8.0 — COKLU KAYNAKTAN tek SWING seviye listesi. SAF fonksiyon (yan etkisiz;
    "simdi" fiyat_seri'nin son zaman damgasindan turetilir -> deterministik, test
    edilebilir). Scalp skor yoluna DOKUNMAZ; cikti yalniz balina_ayarlar'a yazilir.

    Kaynaklar (her seviye 'kaynak' etiketli):
      SWING_PIVOT : _likidite_seviyeleri_bul'dan 24s dip/tepe (zaten hesaplanmis)
      HL          : dun (24s) + hafta (7g) high/low
      LIQ         : likidasyon kumeleri (fiyat-etiketli long/short hacim yogunlasmasi)
      ROUND       : yuvarlak sayilar (60000, 61000...) anlik fiyat menzilinde
      VP          : YAKLASIK hacim profili — POC/VAH/VAL (tick YOK; zaman-agirlikli
                    TPO, "yaklasik" etiketiyle)
      ELLE        : kullanicinin girdigi seviyeler (en yuksek oncelik)

    Girdi:
      anlik_fiyat  : su anki fiyat (ROUND menzili + VP penceresi merkezi)
      fiyat_seri   : [(epoch, fiyat), ...] son ~7 gun
      vol_pct      : esik_volatilite (%); <=0 ise vol-bagimli kaynaklar (LIQ/VP) atlanir
      dipler,tepeler: _likidite_seviyeleri_bul ciktisi ([{'fiyat','test','yas_dk'}])
      liq_kayitlari: [(fiyat, long_liq, short_liq), ...] (LIQ kumeleri icin)
      elle_seviyeler: [{'fiyat',...}] veya [float, ...] (opsiyonel)

    A3 BIRLESTIRME: iki seviye SEVIYE_KUMELEME_VOL x vol_pct (%) bandi icindeyse
    YUKSEK oncelikli KAZANIR; digeri gizli=True (silinmez — panel gizli olmayani gosterir).

    v8 ADIM 1: her seviyeye 'guc' (0-100) + 'kaynaklar' (liste) eklenir. Gorunur
    seviye, kendisine birlesen (gizli) seviyelerin kaynaklarini da devralir; puanlar
    TOPLANIR (SWING_GUC_PUAN, tavan 100). 1s/4s pivot cakismasi (+/-SWING_COKZAMAN_BANT,
    fiyat ORANI) 'COKZAMAN' kaynagi olarak +25 ekler. pivotlar_1s/4s None ise (kline
    cekilemedi) cokzaman puani eklenmez, hata firlatilmaz (A1-3). guc <
    SWING_SEVIYE_MIN_GUC seviyeler haritada KALIR (panel gosterir) ama grab motoru
    (ADIM 2+) onlari YOK SAYAR. Mevcut cikti formati BOZULMAZ — yalniz alan eklenir.

    Donus: [{'fiyat','kaynak','oncelik','not','gizli','guc','kaynaklar', ...}, ...]
    fiyata gore sirali.
    """
    ham = []

    def ekle(fiyat, kaynak, notu='', ekstra=None):
        if not fiyat or fiyat <= 0:
            return
        d = {'fiyat': round(float(fiyat), 1), 'kaynak': kaynak,
             'oncelik': SWING_ONCELIK.get(kaynak, 9), 'not': notu, 'gizli': False}
        if ekstra:
            d.update(ekstra)
        ham.append(d)

    seri = sorted(((t, f) for t, f in (fiyat_seri or []) if f and f > 0),
                  key=lambda x: x[0])
    simdi = seri[-1][0] if seri else None
    af = anlik_fiyat if (anlik_fiyat and anlik_fiyat > 0) else (seri[-1][1] if seri else 0.0)

    # 1) SWING_PIVOT — zaten hesaplanmis 24s dip/tepe
    for d in (dipler or []):
        ekle(d.get('fiyat'), 'SWING_PIVOT', 'dip', {'test': d.get('test')})
    for t in (tepeler or []):
        ekle(t.get('fiyat'), 'SWING_PIVOT', 'tepe', {'test': t.get('test')})

    # 2) HL — dun (24s) ve hafta (7g) high/low
    if seri and simdi is not None:
        for etiket, gun in (('dun', 1), ('hafta', 7)):
            dilim = [f for t, f in seri if t >= simdi - gun * 86400]
            if dilim:
                ekle(max(dilim), 'HL', f'{etiket} H')
                ekle(min(dilim), 'HL', f'{etiket} L')

    # 3) LIQ — likidasyon kumeleri (fiyat-etiketli hacim yogunlasmasi)
    if liq_kayitlari and vol_pct and vol_pct > 0 and af > 0:
        kova_gen = af * (SWING_LIQ_KOVA_VOL * vol_pct) / 100.0
        if kova_gen > 0:
            kovalar = {}
            for satir in liq_kayitlari:
                fiyat = satir[0]
                if not fiyat or fiyat <= 0:
                    continue
                hac = (satir[1] or 0) + (satir[2] or 0)
                k = round(fiyat / kova_gen) * kova_gen
                kovalar[k] = kovalar.get(k, 0.0) + hac
            if kovalar:
                hacimler = sorted(kovalar.values())
                medyan = hacimler[len(hacimler) // 2] or 0.0
                esik = max(medyan * SWING_LIQ_MIN_KAT, 1.0)
                for k, h in kovalar.items():
                    if h >= esik:
                        ekle(k, 'LIQ', 'likidasyon kumesi', {'hacim': round(h, 0)})

    # 4) ROUND — yuvarlak sayilar, anlik fiyat menzilinde (math yok: int-ceil)
    if af > 0:
        menzil = af * (SWING_ROUND_MENZIL_VOL * (vol_pct or 0.1)) / 100.0
        menzil = max(menzil, SWING_ROUND_ADIM)          # en az +/- 1 yuvarlak adim
        r = int((af - menzil) // SWING_ROUND_ADIM) * SWING_ROUND_ADIM
        if r < af - menzil:
            r += SWING_ROUND_ADIM
        ust = af + menzil
        while r <= ust:
            ekle(r, 'ROUND', 'yuvarlak')
            r += SWING_ROUND_ADIM

    # 5) VP — YAKLASIK hacim profili (zaman-agirlikli TPO, tick YOK). Son 24s.
    if seri and simdi is not None and vol_pct and vol_pct > 0 and af > 0:
        kova_gen = af * (SWING_VP_KOVA_VOL * vol_pct) / 100.0
        gunluk = [f for t, f in seri if t >= simdi - 86400]
        if kova_gen > 0 and gunluk:
            prof = {}
            for f in gunluk:
                k = round(f / kova_gen) * kova_gen
                prof[k] = prof.get(k, 0) + 1             # her kayit = 1 zaman birimi
            poc = max(prof, key=lambda kk: prof[kk])
            ekle(poc, 'VP', 'POC (yaklasik)')
            # Value area: POC'tan disa dogru, komsunun buyugunu ekleyerek %70 hacme kadar
            hedef = sum(prof.values()) * SWING_VP_DEGER_ALANI
            sk = sorted(prof)
            i = sk.index(poc)
            lo = hi = i
            biriken = prof[poc]
            while biriken < hedef and (lo > 0 or hi < len(sk) - 1):
                asagi = prof[sk[lo - 1]] if lo > 0 else -1
                yukari = prof[sk[hi + 1]] if hi < len(sk) - 1 else -1
                if yukari >= asagi and hi < len(sk) - 1:
                    hi += 1; biriken += prof[sk[hi]]
                elif lo > 0:
                    lo -= 1; biriken += prof[sk[lo]]
                else:
                    break
            ekle(sk[hi], 'VP', 'VAH (yaklasik)')
            ekle(sk[lo], 'VP', 'VAL (yaklasik)')

    # 6) ELLE — kullanici seviyeleri (en yuksek oncelik)
    for e in (elle_seviyeler or []):
        if isinstance(e, dict):
            ekle(e.get('fiyat'), 'ELLE', e.get('not') or e.get('etiket') or 'elle')
        else:
            ekle(e, 'ELLE', 'elle')

    # --- A3: PRIORITY-MERGE — 1 x vol bandinda YUKSEK oncelik kazanir ---
    # (_likidite_seviyeleri_bul kumeleme formuluyle BIREBIR: |a-b|/b*100 <= KAT*vol)
    band = SEVIYE_KUMELEME_VOL * (vol_pct or 0.0)
    ham.sort(key=lambda d: (d['oncelik'], d['fiyat']))
    korunan = []
    for d in ham:
        cakisti = False
        for kv in korunan:
            ref = kv['fiyat'] or 1.0
            if band > 0:
                if abs(d['fiyat'] - kv['fiyat']) / ref * 100.0 <= band:
                    cakisti = True
                    break
            elif d['fiyat'] == kv['fiyat']:
                cakisti = True
                break
        if cakisti:
            d['gizli'] = True          # yuksek oncelikliye yakin -> gizle (silme)
            # v8: gizlenen kaynagi kazanana DEVRET — cakisan kaynaklarin puanlari toplanacak
            kv.setdefault('kaynaklar', [kv['kaynak']]).append(d['kaynak'])
        else:
            korunan.append(d)

    # ---- v8 ADIM 1: GUC PUANI (0-100) ----
    cok_zaman = [p for p in (list(pivotlar_1s or []) + list(pivotlar_4s or []))
                 if p and p.get('fiyat') and p['fiyat'] > 0]
    # v8 G3: EQ kumeleri ZAMAN DILIMI BASINA ayri hesaplanir (denetim KESIN):
    # onemli bir 4s tepesi ayni fiyatta 1s tepesi de olur — havuz birlestirilseydi
    # TEK fiziksel ekstrem 2 uyeli sahte "EQH" kumesi dogururdu (+20 uydurma guc).
    eq_kumeler = _eq_kumeleri(pivotlar_1s) + _eq_kumeleri(pivotlar_4s)
    for d in ham:
        kaynaklar = list(d.get('kaynaklar') or [d['kaynak']])
        # 1s/4s pivot cakismasi: fiyat ORANI bandi (vol degil — spec boyle)
        if any(abs(d['fiyat'] - p['fiyat']) / d['fiyat'] <= SWING_COKZAMAN_BANT
               for p in cok_zaman):
            kaynaklar.append('COKZAMAN')
        # G3: EQ kumesiyle cakisan seviye +20 (kaynaklar'a 'EQ'; panel rozet gosterir)
        if any(abs(d['fiyat'] - e['fiyat']) / d['fiyat'] <= SWING_COKZAMAN_BANT
               for e in eq_kumeler):
            kaynaklar.append('EQ')
        d['kaynaklar'] = kaynaklar
        d['guc'] = min(100, sum(SWING_GUC_PUAN.get(k, 0) for k in kaynaklar))

    ham.sort(key=lambda d: d['fiyat'])
    return ham


def _seviye_kalicilik(eski_liste, yeni_liste, vol_pct, simdi):
    """
    v8.8-A — SEVIYE KALICILIGI (SADECE KAYIT; kapi/esik DEGIL). SAF.
    'durum.swing_seviyeler = _oto' haritayi komple degistiriyordu — 20 yenileme
    ayakta kalan seviye ile bir kez gorunen ayirt edilemiyordu (olculen: 15.6
    saatte 163 seviye, 331 degisim). Yeni liste eskisiyle eslestirilir:
      * band: MEVCUT SEVIYE_KUMELEME_VOL x vol (yeni 'ayni seviye' tanimi YOK)
      * eslesen  -> ilk_gorulme_ts korunur, yenileme_sayisi += 1
      * yeni     -> ilk_gorulme_ts = simdi, yenileme_sayisi = 1
      * donmeyen -> duser (mezarlik yok)
    yeni_liste YERINDE zenginlesir ve dondurulur. SWING_SEVIYE_MIN_GUC filtresine
    ve karar zincirine DOKUNMAZ (v8.8 mutlak kurali).
    """
    band = SEVIYE_KUMELEME_VOL * (vol_pct or 0.0)
    for y in (yeni_liste or []):
        es = None
        for e in (eski_liste or []):
            ef, yf = e.get('fiyat'), y.get('fiyat')
            if not ef or not yf:
                continue
            if (band > 0 and abs(yf - ef) / ef * 100.0 <= band) or ef == yf:
                if es is None or abs(yf - ef) < abs(yf - es.get('fiyat', 0)):
                    es = e
        if es is not None and es.get('ilk_gorulme_ts'):
            y['ilk_gorulme_ts'] = es['ilk_gorulme_ts']
            y['yenileme_sayisi'] = (es.get('yenileme_sayisi') or 0) + 1
        else:
            y['ilk_gorulme_ts'] = simdi
            y['yenileme_sayisi'] = 1
    return yeni_liste


def _emici_yon(emilim):
    """
    v8.1 — Emilim dict'inden swing YON tahmini: 'LONG'|'SHORT'|None. SAF.
      guclu emilim + satici tukendi + spot BID-agir -> toplama (LONG)
      guclu emilim + alici tukendi  + spot ASK-agir -> dagitim (SHORT)
    Emilim yoksa (esneklik>=YOK) veya yon belirsizse None (sifir tuzagi: uydurmaz)."""
    if not emilim:
        return None
    esnek = emilim.get('emilim_esnekligi')
    if esnek is None or esnek >= EMILIM_YOK_ESIK:
        return None
    sp = emilim.get('spot_egilim')
    if emilim.get('satici_tukenmesi') and sp is not None and sp >= EMILIM_EGILIM_ESIGI:
        return 'LONG'
    if emilim.get('alici_tukenmesi') and sp is not None and sp <= -EMILIM_EGILIM_ESIGI:
        return 'SHORT'
    return None


def _swing_kademe(anlik_fiyat, seviyeler, vol_pct, grab, tasfiye_var, emilim,
                  funding, d_oi):
    """
    v8.1 — KADEMELI SWING durum makinesi. SAF fonksiyon; scalp skor yoluna DOKUNMAZ.

      YOK      : yakin seviye yok / veri yok
      IZLE     : fiyat bir seviyeye SWING_YAKINLIK_VOL x vol yaklasti
      HAZIRLAN : IZLE + (grab basladi VEYA funding asiri VEYA emici yon verdi)
      SINYAL   : 3/3 -> seviye + grab TAMAM + tasfiye teyidi (v9.7: emici YON
                 sarti karardan cikti — kaynagi defter egilimi; kayitta yasar)

    grab: {'yon':'LONG'|'SHORT'|None, 'baslad':bool, 'tamam':bool} (supurme ozeti).
    emilim: emilim dict; funding/d_oi: kalabalik baglami.
    Donus: {'kademe','yon','kademe_skoru'(0-100),'yakin_seviye','mesafe_vol',
            'sartlar','sebepler'}.
    """
    bos = {'kademe': 'YOK', 'yon': None, 'kademe_skoru': 0, 'yakin_seviye': None,
           'mesafe_vol': None, 'sartlar': {}, 'sebepler': []}
    gorunur = [s for s in (seviyeler or []) if not s.get('gizli')]
    if not gorunur or not anlik_fiyat or anlik_fiyat <= 0 or not vol_pct or vol_pct <= 0:
        return bos
    en = min(gorunur, key=lambda s: abs(s['fiyat'] - anlik_fiyat))
    mesafe_vol = abs(en['fiyat'] - anlik_fiyat) / anlik_fiyat * 100.0 / vol_pct
    if mesafe_vol > SWING_YAKINLIK_VOL:
        return {**bos, 'yakin_seviye': en, 'mesafe_vol': round(mesafe_vol, 2)}

    # aday yon uzlasisi: seviye konumu (destek altta->LONG / direnc ustte->SHORT),
    # grab yonu, emici yonu CELISMEMELI. Celiskide yon=None -> SINYAL yok.
    # YON: yalniz GERCEK yonlu sensorlerden (grab + emici). "seviye_yon" (en yakin
    # seviye altta/ustte) KALDIRILDI — fiyat seviyenin uzerinde salinirken LONG<->SHORT
    # cakiliyor, grab'la celisip yonu null'a dusuruyor ve KARAR ANINDA gercek sinyali
    # BLOKLUYORDU (canli veride LONG<->null titremesi gorulmustu). Grab hangi seviyenin
    # supuruldugunu zaten kodluyor; emici emilim yonunu verir. Ikisi CELISIRSE sinyal yok.
    emici = _emici_yon(emilim)
    gyon = grab.get('yon') if grab else None
    # v9.7 (KULLANICI KARARI — Faz 2): emici, YON UZLASISINDAN da cikti. Yarim
    # cikarma tutarsiz olurdu: OB izli emici sinyal URETEMIYORSA IPTAL de
    # EDEMEMELI (celiski vetosu bir karar etkisiydi). Yon artik yalniz grab'dan;
    # celiski tek kaynakla olusamaz (kod korunur — ileride kaynak eklenirse
    # yeniden devreye girer). emici KAYIT/sebep/rozet olarak yasar.
    yonler = [y for y in (gyon,) if y]
    celiski = len(set(yonler)) > 1
    yon = None if celiski else (yonler[0] if yonler else None)

    grab_tamam = bool(grab and grab.get('tamam'))
    grab_basla = bool(grab and (grab.get('baslad') or grab.get('tamam')))
    funding_asiri = abs(funding or 0) > SWING_FUNDING_ASIRI
    emici_var = emici is not None
    sartlar = {'seviye': True, 'grab_tamam': grab_tamam,
               'tasfiye': bool(tasfiye_var), 'emici_yon': emici_var}

    skor = 25                                    # seviyeye yakin
    skor += 25 if grab_tamam else (12 if grab_basla else 0)
    skor += 25 if tasfiye_var else 0
    skor += 25 if emici_var else 0
    skor = min(100, skor)

    sebepler = []
    if grab_tamam:
        sebepler.append(f"grab TAMAM ({gyon or '?'})")
    elif grab_basla:
        sebepler.append("grab basladi")
    if tasfiye_var:
        sebepler.append("tasfiye teyidi (OI coktu)")
    if emici_var:
        sebepler.append(f"emici yon: {emici}")
    if funding_asiri:
        sebepler.append(f"funding asiri ({funding:.4f})")
    if celiski:
        sebepler.append(f"YON CELISKISI {sorted(set(yonler))} -> sinyal yok")

    # v9.7 (KULLANICI KARARI — Faz 2): 'emici yon' SARTI karardan cikti — emici
    # yonu spot/perp defter EGILIMINDEN besleniyordu (60sn REST fotografi; order
    # book karara girmez). SINYAL artik 3/3: seviye(yakinlik) + grab TAMAM +
    # tasfiye. emici OLCULMEYE ve sartlar/sebepler KAYDINA girmeye devam eder
    # (panel rozeti yasar); HAZIRLAN tetigi olarak da kalir (kayit kademesi).
    dort_dort = (grab_tamam and bool(tasfiye_var)
                 and not celiski and yon is not None)
    if dort_dort:
        kademe = 'SINYAL'
    elif grab_basla or funding_asiri or emici_var:
        kademe = 'HAZIRLAN'
    else:
        kademe = 'IZLE'
    return {'kademe': kademe, 'yon': yon, 'kademe_skoru': skor, 'yakin_seviye': en,
            'mesafe_vol': round(mesafe_vol, 2), 'sartlar': sartlar, 'sebepler': sebepler}


def _swing_hedef_stop(yon, giris, seviyeler, vol_pct, magnet=None,
                      stop_zorla=None, min_guc=None):
    """
    v8.1 — Hedef + stop YAPIDAN uretir (sabit %% ASLA). SAF fonksiyon.
    SHORT: kisa_hedef=bir alt seviye; swing_hedef=magnet (en yogun liq kumesi) ya da
    en uzak alt seviye; stop=bir UST YAPISAL seviye + SWING_STOP_TAMPON_VOL x vol.
    LONG simetrik. v9.2: R/R kapisi HER IKI modda rr_KISA uzerinden —
    rr_kisa < SWING_MIN_RR -> gecerli=False (ilk seviyeye bile 2R yoksa sinyal yok;
    rr_swing salt kayit).

    v8 GRAB MODU (ADIM 5; stop_zorla verilirse — mevcut fonksiyon GENISLETILDI,
    ikiz yazilmadi):
      * stop  = stop_zorla (fitil ucu/kirilan seviye + tampon; _grab_stop hesaplar)
      * min_guc verilirse hedef adaylari 'guc' >= min_guc seviyelerle sinirlanir
      * kisa_hedef = hedef yonunde ILK guclu seviye; swing_hedef = KARSI LIKIDITE
        HAVUZU: hedef yonunde EN YUKSEK guc'lu seviye (esitlikte uzak olan —
        "dip supuruldyse yuksekleri hedefle" miknatis ilkesi); magnet YOK SAYILIR
      * R/R kapisi rr_KISA uzerinden: rr_kisa < SWING_MIN_RR -> gecerli=False (rr_red)
    Donus: {'kisa_hedef','swing_hedef','stop','rr_kisa','rr_swing','gecerli','sebep'}."""
    bos = {'kisa_hedef': None, 'swing_hedef': None, 'stop': None, 'rr_kisa': None,
           'rr_swing': None, 'gecerli': False, 'sebep': 'girdi yetersiz'}
    grab_modu = stop_zorla is not None
    # vol_pct yalniz ESKI yolun stop tamponu icin gerekir; grab modunda stop hazir
    # gelir (fitil+ATR tamponu) — vol henuz olculemedi diye gecerli grab sinyali
    # oldurulmez (denetim: restart sonrasi esik_volatilite=0 penceresi).
    if yon not in ('LONG', 'SHORT') or not giris or giris <= 0 \
            or (not grab_modu and (not vol_pct or vol_pct <= 0)):
        return bos
    gorunur = [s for s in (seviyeler or []) if not s.get('gizli')]
    hedef_havuzu = [s for s in gorunur
                    if min_guc is None or (s.get('guc') or 0) >= min_guc]
    tampon = giris * (SWING_STOP_TAMPON_VOL * (vol_pct or 0.0)) / 100.0

    def _en_guclu(adaylar):
        # en yuksek guc; esitlikte girise UZAK olan (karsi havuz miknatisi)
        return max(adaylar, key=lambda s: ((s.get('guc') or 0),
                                           abs(s['fiyat'] - giris)))['fiyat']

    if yon == 'SHORT':
        altlar = sorted((s for s in hedef_havuzu if s['fiyat'] < giris),
                        key=lambda s: -s['fiyat'])                 # en yakin alt once
        kisa = altlar[0]['fiyat'] if altlar else None
        if grab_modu:
            stop = stop_zorla
            swing = _en_guclu(altlar) if altlar else None
        else:
            ustler = sorted((s for s in gorunur if s['fiyat'] > giris and s['kaynak'] in SWING_YAPISAL),
                            key=lambda s: s['fiyat'])              # en yakin ust yapisal once
            stop = (ustler[0]['fiyat'] + tampon) if ustler else None
            swing = magnet if (magnet and magnet < giris) else (altlar[-1]['fiyat'] if altlar else None)
        if kisa is None or swing is None or stop is None or stop <= giris:
            return {**bos, 'sebep': 'yapisal seviye eksik (alt hedef / ust stop yok)'}
        risk = stop - giris
        rr_kisa = (giris - kisa) / risk
        rr_swing = (giris - swing) / risk
    else:  # LONG
        ustler = sorted((s for s in hedef_havuzu if s['fiyat'] > giris),
                        key=lambda s: s['fiyat'])
        kisa = ustler[0]['fiyat'] if ustler else None
        if grab_modu:
            stop = stop_zorla
            swing = _en_guclu(ustler) if ustler else None
        else:
            altlar = sorted((s for s in gorunur if s['fiyat'] < giris and s['kaynak'] in SWING_YAPISAL),
                            key=lambda s: -s['fiyat'])
            stop = (altlar[0]['fiyat'] - tampon) if altlar else None
            swing = magnet if (magnet and magnet > giris) else (ustler[-1]['fiyat'] if ustler else None)
        if kisa is None or swing is None or stop is None or stop >= giris:
            return {**bos, 'sebep': 'yapisal seviye eksik (ust hedef / alt stop yok)'}
        risk = giris - stop
        rr_kisa = (kisa - giris) / risk
        rr_swing = (swing - giris) / risk
    # v9.2: R/R kapisi HER IKI modda rr_KISA uzerinden (birlesik kapi). Canli veri
    # (23-25 Tem, n=85 HAZIRLAN kurulumu): rr_swing kapisi 77/85 geciriyordu
    # (medyan 17.1 — uzak miknatis hedefi kapiyi lastik damgaya ceviriyor),
    # rr_kisa kapisi 30/85 gecirir (gecenlerin medyani 3.6 — gercek 2R+ kalite).
    # rr_swing KAYIT olarak kalir (kohort/arsiv/backtest), artik kapiya girmez.
    gecerli = rr_kisa >= SWING_MIN_RR
    sebep = 'ok' if gecerli else f'rr_kisa {round(rr_kisa, 2)} < {SWING_MIN_RR}'
    return {'kisa_hedef': round(kisa, 1), 'swing_hedef': round(swing, 1),
            'stop': round(stop, 1), 'rr_kisa': round(rr_kisa, 2),
            'rr_swing': round(rr_swing, 2), 'gecerli': gecerli, 'sebep': sebep}


def _grab_ozeti(dip_durumlari, tepe_durumlari, simdi):
    """
    v8.7 — Supurme durum makinelerinden SWING grab ozeti. SAF fonksiyon.

    Denetim (2/2 KESIN) iki kusuru dogruladi, ikisi burada duzeltilir:
    1) SILAHLI 'baslad' SAYILMAZ: SILAHLI = fiyat seviyeye yakin (salt yakinlik) —
       yakinlik zaten IZLE kademesinin isi. SILAHLI'yi baslad saymak, pivot
       yakininda HAZIRLAN'i SUREKLI aciyordu (canli: skor 37 sabit). grab ancak
       DELINDI (fitil gercekten deldi) ile baslar.
    2) ONAYLI'ya TAZELIK suzgeci: scalp yolu onay_ts <= SUPURME_GECERLILIK_DK
       suzer; swing ozeti suzmuyordu -> yetim ONAYLI 60 dk 'tamam' sayilip
       sahte SINYAL acabilirdi. Ayni suzgec burada da uygulanir.
    Iki taraf (dip+tepe) ayni anda aktifse yon=None (celiski) -> SINYAL kapanir.
    """
    def _taraf(durumlar):
        tamam = any(d.get('durum') == 'ONAYLI'
                    and (simdi - (d.get('onay_ts') or 0)) <= SUPURME_GECERLILIK_DK * 60
                    for d in (durumlar or {}).values())
        baslad = tamam or any(d.get('durum') == 'DELINDI'
                              for d in (durumlar or {}).values())
        return tamam, baslad
    dip_tamam, dip_baslad = _taraf(dip_durumlari)
    tepe_tamam, tepe_baslad = _taraf(tepe_durumlari)
    if dip_baslad and tepe_baslad:
        return {'yon': None, 'baslad': True, 'tamam': (dip_tamam or tepe_tamam)}
    if dip_baslad:
        return {'yon': 'LONG', 'baslad': True, 'tamam': dip_tamam}
    if tepe_baslad:
        return {'yon': 'SHORT', 'baslad': True, 'tamam': tepe_tamam}
    return {'yon': None, 'baslad': False, 'tamam': False}


# ======================= v8: LIQ GRAB SWING MOTORU — SAF CEKIRDEK =======================
# ADIM 2-5'in tum karar mantigi asagidaki SAF fonksiyonlardadir (yan etkisiz, test
# edilebilir). Veri beslemesi: coinalyze thread'i 15dk/1s/4s kline ceker, ozet dongusu
# her turda "yeni KAPALI 15dk mumu var mi?" diye sorar. Scalp skor yoluna DOKUNMAZ.

def _kline_kapali(mumlar, simdi, periyot_sn):
    """v8 — SADECE KAPALI mumlar: t + periyot <= simdi (GK-8: dizinin son elemani
    acik olabilir; karar icin kapali olanlar gecerli). Alanlar float'a cevrilir,
    bozuk kayit atlanir. Donus: zaman-sirali [{'t','o','h','l','c','v'}]."""
    out = []
    for m in (mumlar or []):
        try:
            t = float(m.get('t') or 0)
            if t > 0 and t + periyot_sn <= simdi:
                out.append({'t': t, 'o': float(m.get('o') or 0), 'h': float(m.get('h') or 0),
                            'l': float(m.get('l') or 0), 'c': float(m.get('c') or 0),
                            'v': float(m.get('v') or 0)})
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda m: m['t'])
    return out


def _kline_pivotlar(mumlar):
    """v8 — 3-mum pivot kurali (KAPALI mumlar): h[i] iki komsusundan yuksekse swing
    high ('H'), l[i] iki komsusundan dusukse swing low ('L'). 1s/4s cok-zaman
    cakismasi (ADIM 1) icin. Donus: [{'fiyat','tur','ts'}]."""
    out = []
    ms = mumlar or []
    for i in range(1, len(ms) - 1):
        a, b, c = ms[i - 1], ms[i], ms[i + 1]
        if b['h'] > 0 and b['h'] > a['h'] and b['h'] > c['h']:
            out.append({'fiyat': round(b['h'], 1), 'tur': 'H', 'ts': b['t']})
        if b['l'] > 0 and b['l'] < a['l'] and b['l'] < c['l']:
            out.append({'fiyat': round(b['l'], 1), 'tur': 'L', 'ts': b['t']})
    return out


def _atr15(mumlar):
    """v8 — ATR: son 14 KAPALI mumun true-range ortalamasi (TR icin bir onceki
    kapanis gerekir -> en az 15 mum). Hesaplanamiyorsa None — sifir tuzagi:
    ATR yokken aday URETILMEZ (A2-4), 0 uydurulmaz."""
    ms = mumlar or []
    if len(ms) < 15:
        return None
    trler = []
    for i in range(len(ms) - 14, len(ms)):
        onceki_c = ms[i - 1]['c']
        h, l = ms[i]['h'], ms[i]['l']
        if h <= 0 or l <= 0 or onceki_c <= 0:
            return None
        trler.append(max(h - l, abs(h - onceki_c), abs(l - onceki_c)))
    return sum(trler) / len(trler)


def _sweep_adayi(mum, seviye, guc, atr15, hacim_ort20, lik_pencere_toplam,
                 son_aday_ts, simdi):
    """
    v8 ADIM 2+3 — 15dk KAPALI mumda sweep ADAYI + kapanis karari. SAF. Aday != sinyal.

    ADIM 2 (aday sartlari — hepsi):
      * delme: high > seviye + delme_min (SHORT-yonlu sweep = yukari fitil) VEYA
               low < seviye - delme_min (LONG-yonlu). delme_min = max(
               SWEEP_MIN_DELME_PCT x kapanis, 0.2 x ATR15). ATR None -> aday YOK.
      * eslik (en az biri): 15dk penceresinde likidasyon toplami > 0 VEYA
               mum hacmi > SWEEP_ESLIK_HACIM_KAT x son 20 mum ort. hacmi.
      * cooldown: ayni seviyede SWEEP_COOLDOWN_DK icinde ikinci aday uretilmez.
    ADIM 3 (ayni kapali mumda — mum ici ASLA):
      * SHORT-sweep: close < seviye -> DONUS adayi; close > seviye -> DEVAM adayi.
        LONG-sweep simetrik. DONUS icin GOVDE sarti: |close - seviye| >=
        SWEEP_GOVDE_ORAN x (high - low); kil payi -> kapanis_tipi None (sadece kayit).

    Donus: None (aday degil) veya dict{yon: sweep yonu ('SHORT'=yukari fitil,
    'LONG'=asagi fitil — DONUS islem yonuyle ayni adlandirma), kapanis_tipi
    ('DONUS'|'DEVAM'|None), sweep_seviye, sweep_guc, fitil_ucu, delme_derinligi,
    kapanis, kapanis_mesafe_pct, mum_ts, eslik{likidasyon, hacim_kat}}.
    """
    if not mum or not seviye or seviye <= 0:
        return None
    if atr15 is None or atr15 <= 0:
        return None                       # A2-4: ATR olculemedi -> uydurma yok, aday yok
    if son_aday_ts and (simdi - son_aday_ts) < SWEEP_COOLDOWN_DK * 60:
        return None                       # A2-3: cooldown
    h, l, c = mum.get('h') or 0, mum.get('l') or 0, mum.get('c') or 0
    if h <= 0 or l <= 0 or c <= 0:
        return None
    pct_terim = SWEEP_MIN_DELME_PCT * c
    atr_terim = 0.2 * atr15
    delme_min = max(pct_terim, atr_terim)
    # Denetim (KESIN): delme sarti tek basina YETMEZ — mum araligi seviyeyi
    # GERCEKTEN kesmeli (l <= seviye <= h). Yoksa mumun cok altindaki/ustundeki
    # her guclu seviye "sweep" sayilir, sahte DEVAM adaylari gercek sinyali
    # golgeleyip kohortu copluge cevirirdi.
    if h > seviye + delme_min and l <= seviye:
        yon, fitil_ucu, delme = 'SHORT', h, h - seviye
    elif l < seviye - delme_min and h >= seviye:
        yon, fitil_ucu, delme = 'LONG', l, seviye - l
    else:
        return None                       # A2-1: delme yetersiz / seviye kesilmedi
    lik_ok = lik_pencere_toplam is not None and lik_pencere_toplam > 0
    hacim_kat = (mum.get('v') / hacim_ort20) \
        if (hacim_ort20 and hacim_ort20 > 0 and mum.get('v')) else None
    hacim_ok = hacim_kat is not None and hacim_kat > SWEEP_ESLIK_HACIM_KAT
    if not (lik_ok or hacim_ok):
        return None                       # A2-2: eslik yok -> aday degil
    aralik = h - l
    if yon == 'SHORT':
        tip = 'DONUS' if c < seviye else 'DEVAM'
    else:
        tip = 'DONUS' if c > seviye else 'DEVAM'
    if tip == 'DONUS' and aralik > 0 and abs(c - seviye) < SWEEP_GOVDE_ORAN * aralik:
        tip = None                        # A3-3: kil payi kapanis -> belirsiz, sinyal yolu kapali
    return {'yon': yon, 'kapanis_tipi': tip,
            'sweep_seviye': round(float(seviye), 1), 'sweep_guc': guc,
            'fitil_ucu': round(fitil_ucu, 1), 'delme_derinligi': round(delme, 1),
            'kapanis': round(c, 1),
            'kapanis_mesafe_pct': round(abs(c - seviye) / seviye * 100.0, 4),
            'mum_ts': mum.get('t'),
            'eslik': {'likidasyon': round(lik_pencere_toplam, 0) if lik_pencere_toplam is not None else None,
                      'hacim_kat': round(hacim_kat, 2) if hacim_kat is not None else None},
            # v8.8-B: delme esigi teshisi — SADECE KAYIT, karara girmez. Olculen:
            # ATR terimi hic baglayici olmamisti (15dk 0/49) — esik fiilen sabit
            # %0.08'di; hangi terimin kazandigi artik gorunur.
            'teshis': {'delme_min_belirleyen': 'ATR' if atr_terim > pct_terim else 'PCT',
                       'delme_min_pct_terim': round(pct_terim, 2),
                       'delme_min_atr_terim': round(atr_terim, 2),
                       'delme_atr_kati': round(delme / atr15, 2)}}


def _grab_teshis(aday, seviye, pencere, pencere_kayitlari, med_long, med_short, simdi):
    """
    v8.8-B — ADAY TESHIS ALANLARI (SADECE KAYIT; hicbiri aday/sinyal kararina
    girmez). SAF. Mevcut ciktilardan derler, IKIZ HESAP YAZMAZ:
      * lik yogunluklari: pencere.lik_*_yog_max — bunlar ZATEN likidasyon_yogunlugu()
        ciktilaridir (dakika izine skor_girdi'den yazilir). medyan yok/0 -> None
        (likidasyon_yogunlugu medyansizken 0.0 dondurur — skor yolu davranisi
        DEGISMEZ; teshis katmani 'olculemedi'yi medyani kontrol ederek ayirir).
      * OI: pencere.d_oi_pct / d_oi_5dk_min_pct oldugu gibi; oi_kapisi_gecti =
        _tasfiye_bayraklari (mevcut fonksiyon; _sweep_teyit'in DONUS ayagiyla
        AYNI degerlendirme) — olculemiyorsa None.
      * faz: fitil_ts = delmenin dakika izinde ILK gorunusu (dakika ornekleme
        fitili kacirabilir -> None; uydurma yok). mum_ici_konum = 0..1.
    """
    sf = aday.get('sweep_seviye')
    mts = aday.get('mum_ts')
    syn = aday.get('yon')
    # --- seviye kimligi (A'dan gelen kalicilik alanlariyla) ---
    igt = (seviye or {}).get('ilk_gorulme_ts')
    out = {'seviye_guc': (seviye or {}).get('guc'),
           'seviye_kaynaklari': list((seviye or {}).get('kaynaklar') or []) or None,
           'seviye_yasi_dk': round((simdi - igt) / 60.0, 1) if igt else None,
           'seviye_yenileme_sayisi': (seviye or {}).get('yenileme_sayisi')}
    # --- likidasyon teshisi (yon-esli; medyan yoksa None, sifir DEGIL) ---
    yl = (pencere or {}).get('lik_long_yog_max')
    ys = (pencere or {}).get('lik_short_yog_max')
    yog_long = yl if (med_long and med_long > 0 and yl is not None) else None
    yog_short = ys if (med_short and med_short > 0 and ys is not None) else None
    out['lik_yog_yon'] = yog_short if syn == 'SHORT' else yog_long
    out['lik_yog_ters'] = yog_long if syn == 'SHORT' else yog_short
    out['lik_iki_tarafli'] = ((yog_long >= TASFIYE_DIKEN_CARPANI
                               and yog_short >= TASFIYE_DIKEN_CARPANI)
                              if (yog_long is not None and yog_short is not None)
                              else None)
    # --- OI teshisi (_sweep_teyit DONUS ayagiyla ayni degerlendirme; kayit amacli) ---
    out['d_oi_pct'] = (pencere or {}).get('d_oi_pct')
    out['d_oi_5dk_min_pct'] = (pencere or {}).get('d_oi_5dk_min_pct')
    d5 = out['d_oi_5dk_min_pct']
    if d5 is not None and not (yl is None and ys is None):
        out['oi_kapisi_gecti'] = _tasfiye_bayraklari(yl or 0.0, ys or 0.0, d5)[0]
    else:
        out['oi_kapisi_gecti'] = None
    # --- faz teshisi: delmenin dakika izindeki ilk gorunusu ---
    fitil_ts = None
    if sf and mts is not None:
        for r in (pencere_kayitlari or []):
            ts, f = r.get('ts'), r.get('fiyat')
            if ts is None or not f or not (mts <= ts < mts + 900):
                continue
            if (syn == 'SHORT' and f > sf) or (syn == 'LONG' and f < sf):
                fitil_ts = ts
                break
    out['fitil_ts'] = round(fitil_ts, 0) if fitil_ts is not None else None
    out['mum_ici_konum'] = (round((fitil_ts - mts) / 900.0, 3)
                           if (fitil_ts is not None and mts is not None) else None)
    return out


def _lik_penceresi_ayristir(data):
    """
    v8.9-B — Coinalyze likidasyon cevabinin ayristirilmasi. SAF (D4-D6 testleri
    dogrudan cagirir); parse mantigi v8.8-E'deki satirlarla BIREBIR (float
    donusumu, damga max'i, borsa sayimi) — yalnizca sona SIFIR TUZAGI eklendi:
    liste donup HICBIR borsada history yoksa lik_borsa None'dir ("0 borsa olctu"
    degil "OLCULEMEDI"; olculen: 772 satirin 141'i = %32 boyle kordu ve korluk
    'sifir likidasyon' diye kaydedilip TASFIYE'yi yapisal olarak imkansiz
    kiliyordu — kohort sessizce kirleniyordu).
    agg toplamlari DEGISMEZ (0.0 kalir): onlar karar zincirine girer
    (lik_ok / likidasyon_yogunlugu / _tasfiye_bayraklari) — None yazmak sinyal
    davranisini degistirirdi (v8.8-E'nin uyardigi tuzagin aynisi). Korluk
    ISARETLENIR, veri DEGISTIRILMEZ; kapi karari Faz 2'de.
    Donus: (long_liq, short_liq, lik_damga, lik_borsa).
    """
    long_liq = 0.0
    short_liq = 0.0
    lik_damga = None
    lik_borsa = 0
    for borsa in (data or []):
        hist = borsa.get('history', [])
        if hist:
            lik_borsa += 1
        for nokta in hist:
            long_liq += float(nokta.get('l', 0) or 0)
            short_liq += float(nokta.get('s', 0) or 0)
            _nt = nokta.get('t')
            if _nt:
                lik_damga = max(lik_damga or 0, int(_nt))
    if lik_borsa == 0:
        lik_borsa = None
    return long_liq, short_liq, lik_damga, lik_borsa


def _lik_donma_guncelle(onceki, simdiki, sayac):
    """
    v8.8-E — LIKIDASYON DONMA TESPITI (SADECE KAYIT; karar zincirine GIRMEZ).
    Olculen: liquidation_pool_volume gunde 48 blokta 3+ dk birebir ayni kaldi —
    Coinalyze 5dk penceresi yenilenmiyor. Donmus olcum "ayni kaldi" degil
    "OLCULEMEDI"dir; ama mevcut alana None YAZILMAZ (spec E'nin tuzak uyarisi:
    _sweep_adayi'nin lik_ok kapisi None'da False olur -> aday olumu -> Faz 1
    ihlali). Donma bilgisi AYRI alana (sayac) gider; kapi karari Faz 2'de.
    onceki/simdiki: (long, short, pencere_damgasi). Damga yoksa tespit
    YAPILAMAZ -> 0 (sayac bir tespit sayacidir, olcum degil). SAF.
    """
    if not onceki or not simdiki:
        return 0
    if simdiki[2] is None or onceki[2] is None:
        return 0
    if onceki == simdiki:
        return sayac + 1
    return 0


def _grab_n1_kayitlari(bekleyenler, mum, pencere):
    """
    v8.8-C — SONRAKI MUM SONUCU (GRAB_ADAY_N1): paralel kohort, SINYAL URETMEZ.
    ADIM 3'un ayni-mum sarti kurulumlarin ~2/3'unu gorunmez birakiyordu (olculen:
    ayni mumda donen 13 aday, sonraki mumda donen 28) ve gorunenler sistematik
    olarak en hizli/sert olaylardi — mevcut kohort TARAFLI. Bu kayit, N mumunda
    DEVAM/None siniflanan adayin N+1 kapanisini olcer:
      * N+1 kapanis seviyenin DIGER tarafindaysa GRAB_ADAY_N1 kaydi acilir
      * teyit MEVCUT _sweep_teyit ile, N+1 muminin penceresi verilerek (kopya yok)
      * ust-duzey hedef/stop/rr/yon anahtari YOK (_swing_backtest sinyal sanmasin —
        GRAB_ADAY kaydindaki korumanin aynisi)
    Yalniz TAM bir onceki mumun (mum_ts == mum.t - 900) bekleyenleri islenir;
    eskiler duser. Restart'ta liste bosalir — o mumun N1 olcumu kaybedilir,
    kabul edilebilir (uydurma yerine eksik). SAF.
    """
    out = []
    t = (mum or {}).get('t')
    c = (mum or {}).get('c')
    if t is None or not c or c <= 0:
        return out
    for b in (bekleyenler or []):
        if b.get('mum_ts') != t - 900:
            continue
        sf, syn = b.get('seviye'), b.get('yon')
        if not sf or syn not in ('SHORT', 'LONG'):
            continue
        donus = (c < sf) if syn == 'SHORT' else (c > sf)
        if not donus:
            continue
        ham = dict(b.get('ham') or {})
        ham['n1_kapanis'] = round(c, 1)
        ham['n1_mum_ts'] = t
        ham['n1_teyit'] = _sweep_teyit(syn, 'DONUS', pencere)
        out.append({'tetik': 'GRAB_ADAY_N1', 'seviye': sf,
                    'skor': ham.get('sweep_guc'), 'ham': ham})
    return out


def _grab_pencere_ozeti(seri, t0, t1):
    """
    v8 ADIM 4 penceresi — sweep mumunun 15 dakikasindaki dakikalik kayitlardan ozet.
    SAF. seri: gecmis_seri kayitlari (ozet dongusu her dakika 'lik_long/lik_short/
    lik_*_yog/rejim/emici_yon/alici_tuk/satici_tuk' alanlarini ekler).
    Kapsama < %60 (kayit kopmasi) -> eksik=True -> teyit YAPILMAZ (sinyal yok).
    Olculemeyen alanlar None kalir (sifir tuzagi).
    """
    rows = [r for r in (seri or []) if t0 <= (r.get('ts') or 0) < t1]
    beklenen = max(1, int((t1 - t0) / 60))
    out = {'eksik': len(rows) < max(3, int(beklenen * 0.6)), 'kayit_sayisi': len(rows),
           'd_oi_pct': None, 'd_oi_5dk_min_pct': None, 'd_vadeli_cvd': None,
           'lik_toplam': None, 'lik_long_yog_max': None, 'lik_short_yog_max': None,
           'emici_yonler': [], 'rejimler': [], 'alici_tuk': None, 'satici_tuk': None}
    if not rows:
        return out
    ois = [(r.get('ts'), r.get('oi')) for r in rows if r.get('oi')]
    if len(ois) >= 2 and ois[0][1] > 0:
        out['d_oi_pct'] = (ois[-1][1] - ois[0][1]) / ois[0][1] * 100.0
        # Denetim: TASFIYE_OI_MIN_PCT 5-DK penceresi icin kalibredir; 15dk net
        # farka uygulamak esigi sessizce gevsetir. "Pencerede OI anlamli dustu mu"
        # sorusu MEVCUT tanimin kendi olceginde sorulur: pencere icindeki EN SERT
        # 5dk degisimi (240-420sn araliginda eslesen ornek ciftleri).
        d5 = None
        for j in range(1, len(ois)):
            tj, oj = ois[j]
            es = None
            for ti, oi_v in ois[:j]:
                if 240 <= tj - ti <= 420:
                    es = oi_v              # ti artan: en yakin (son) eslesme kalir
            if es and es > 0:
                pct = (oj - es) / es * 100.0
                d5 = pct if d5 is None else min(d5, pct)
        out['d_oi_5dk_min_pct'] = d5
    # Vadeli CVD deltasi ancak kaynak TUTARLIYSA (v7.8 dersi: kaynaklar arasi delta yasak)
    cv = [(r.get('vadeli_cvd'), r.get('cvd_kaynak')) for r in rows
          if r.get('vadeli_cvd') is not None]
    if len(cv) >= 2 and len({k for _, k in cv}) == 1:
        out['d_vadeli_cvd'] = cv[-1][0] - cv[0][0]
    # Likidasyon: OLCULEN (None olmayan) ornekler toplanir. Coinalyze kesintisinde
    # dakika izi None yazar -> toplam None kalir (sifir tuzagi: "olculemedi",
    # "likidasyon yok" DEGIL). 5dk pencereli orneklerin toplami sisiriktir —
    # yalniz ">0" sorusuna cevap verir.
    likler = [(r.get('lik_long') or 0) + (r.get('lik_short') or 0)
              for r in rows
              if r.get('lik_long') is not None or r.get('lik_short') is not None]
    if likler:
        out['lik_toplam'] = sum(likler)
    yog_l = [r.get('lik_long_yog') for r in rows if r.get('lik_long_yog') is not None]
    yog_s = [r.get('lik_short_yog') for r in rows if r.get('lik_short_yog') is not None]
    if yog_l:
        out['lik_long_yog_max'] = max(yog_l)
    if yog_s:
        out['lik_short_yog_max'] = max(yog_s)
    out['emici_yonler'] = [r.get('emici_yon') for r in rows if r.get('emici_yon')]
    out['rejimler'] = [r.get('rejim') for r in rows if r.get('rejim')]
    # Tukenme bayraklari UC DURUMLU: hic olculmediyse None (sifir tuzagi),
    # olculduyse any(True) — None'lar False uydurulmaz.
    for _alan in ('alici_tuk', 'satici_tuk'):
        _degerler = [r.get(_alan) for r in rows if r.get(_alan) is not None]
        out[_alan] = (any(_degerler) if _degerler else None)
    return out


def _sweep_teyit(sweep_yon, kapanis_tipi, pencere):
    """
    v8 ADIM 4 — ORDER FLOW TEYIDI (sinyal kapisi). SAF. Fitil + kapanis tek basina
    YETMEZ; teyitsiz -> sinyal YOK, sadece kayit.

    DONUS: 3 kriterden en az 2'si, OI ZORUNLU (OI yoksa asla DONUS):
      1) OI cokusu — MEVCUT tasfiye tanimiyla (_tasfiye_bayraklari; yeni esik YOK:
         diken tek basina tasfiye sayilmaz — G1c ile tutarli).
      2) Delta divergence — pencerede d_vadeli_cvd fitil yonune TERS isaret.
      3) Emici ters yon — short-sweep: DAGITIM_AILESI rejimi VEYA alici_tukenmesi
         VEYA _emici_yon=='SHORT' izi (long-sweep simetrik).
    DEVAM: 3'u de ZORUNLU: OI ARTISI + delta kirilim yonuyle AYNI + emici ayni
    yon VEYA YOK (TERS emici -> iptal).
    Pencere eksik/olculemedi -> ilgili kriter None; pencere.eksik -> sonuc None.

    Donus: {'oi','delta','emici','sonuc'} — kriterler True/False/None,
    sonuc in {'GRAB_DONUS','GRAB_DEVAM',None}.
    """
    bos = {'oi': None, 'delta': None, 'emici': None, 'sonuc': None}
    if kapanis_tipi not in ('DONUS', 'DEVAM') or sweep_yon not in ('SHORT', 'LONG'):
        return bos
    if not pencere or pencere.get('eksik'):
        return bos                        # A4-4: pencere verisi eksik -> None, cokme yok
    d_oi = pencere.get('d_oi_pct')
    d_cvd = pencere.get('d_vadeli_cvd')
    rejimler = pencere.get('rejimler') or []
    emici_yonler = pencere.get('emici_yonler') or []
    if kapanis_tipi == 'DONUS':
        # OI cokusu MEVCUT tanimin KENDI olceginde: pencere icindeki en sert 5dk
        # OI degisimi (TASFIYE_OI_MIN_PCT 5dk icin kalibre — denetim bulgusu).
        # Diken tarafi hic OLCULEMEDIYSE (likidasyon izi None) kriter None —
        # "likidasyon yok" uydurulmaz (sifir tuzagi).
        d5 = pencere.get('d_oi_5dk_min_pct')
        yl = pencere.get('lik_long_yog_max')
        ys = pencere.get('lik_short_yog_max')
        oi_k = None
        if d5 is not None and not (yl is None and ys is None):
            oi_k, _ = _tasfiye_bayraklari(yl or 0.0, ys or 0.0, d5)
        delta_k = None
        if d_cvd is not None:
            delta_k = (d_cvd < 0) if sweep_yon == 'SHORT' else (d_cvd > 0)
        # Tukenme yalniz OLCULMUS True ise kanit sayilir (None kanit degildir)
        # v9.7: emici_yonler IZI karardan cikti (kaynagi defter EGILIMI — 60sn
        # REST fotografi; kullanici karari: order book karara girmez). Kriterin
        # CVD tabanli parcalari (rejim ailesi + tukenme) AYNEN kanittir.
        if sweep_yon == 'SHORT':
            emici_k = (any(r in DAGITIM_AILESI for r in rejimler)
                       or pencere.get('alici_tuk') is True)
        else:
            emici_k = (any(r in TOPLAMA_AILESI for r in rejimler)
                       or pencere.get('satici_tuk') is True)
        say = sum(1 for k in (oi_k, delta_k, emici_k) if k is True)
        sonuc = 'GRAB_DONUS' if (oi_k is True and say >= 2) else None
        return {'oi': oi_k, 'delta': delta_k, 'emici': emici_k, 'sonuc': sonuc}
    # DEVAM — kirilim yonu, sweep etiketinin TERSI islem yonudur
    # (yukari fitil DEVAM = yukari kirilim = LONG islem)
    kir_yon = 'LONG' if sweep_yon == 'SHORT' else 'SHORT'
    oi_k = (d_oi > 0) if d_oi is not None else None
    delta_k = None
    if d_cvd is not None:
        delta_k = (d_cvd > 0) if kir_yon == 'LONG' else (d_cvd < 0)
    ters_yon = 'SHORT' if kir_yon == 'LONG' else 'LONG'
    ters_aile = DAGITIM_AILESI if kir_yon == 'LONG' else TOPLAMA_AILESI
    ters_tuk = pencere.get('alici_tuk') if kir_yon == 'LONG' else pencere.get('satici_tuk')
    # ters_tuk None = olculemedi -> karsit KANIT uydurulmaz (is True)
    # v9.7: ters-emici iptalinin defter-egilimi parcasi (emici_yonler) karardan
    # cikti; CVD tabanli ters kanitlar (rejim ailesi + tukenme) iptal etmeye
    # devam eder (kullanici karari: order book karara girmez).
    emici_k = not (any(r in ters_aile for r in rejimler) or ters_tuk is True)
    sonuc = 'GRAB_DEVAM' if (oi_k is True and delta_k is True and emici_k is True) else None
    return {'oi': oi_k, 'delta': delta_k, 'emici': emici_k, 'sonuc': sonuc}


def _grab_stop(sinyal_tipi, sweep_yon, fitil_ucu, seviye, giris, atr15):
    """
    v8 ADIM 5 — STOP: yapisal + tampon (TAM ucta degil — sweep gurultusu payi).
    tampon = max(SWEEP_STOP_TAMPON_PCT x giris, 0.1 x ATR15).
      GRAB_DONUS-short: fitil_ucu + tampon | GRAB_DONUS-long: fitil_ucu - tampon
      GRAB_DEVAM: kirilan seviyenin DIGER tarafi -/+ tampon
    ATR/giris olculemiyorsa None (sifir tuzagi)."""
    if atr15 is None or atr15 <= 0 or not giris or giris <= 0:
        return None
    tampon = max(SWEEP_STOP_TAMPON_PCT * giris, 0.1 * atr15)
    if sinyal_tipi == 'GRAB_DONUS':
        if not fitil_ucu or fitil_ucu <= 0:
            return None
        return round(fitil_ucu + tampon, 1) if sweep_yon == 'SHORT' \
            else round(fitil_ucu - tampon, 1)
    if sinyal_tipi == 'GRAB_DEVAM':
        if not seviye or seviye <= 0:
            return None
        # yukari fitil DEVAM (LONG islem) -> stop seviye ALTI; simetrik
        return round(seviye - tampon, 1) if sweep_yon == 'SHORT' \
            else round(seviye + tampon, 1)
    return None


# ---------------- v8 GUCLENDIRICILER (G1 FVG / G2 CHoCH / G3 EQ) ----------------
# G1 ve G2 SADECE KAYIT'tir: girisi/sinyali DEGISTIRMEZ. 20+ olay birikince SQL ile
# "FVG'li vs FVG'siz" / "CHoCH'lu vs CHoCH'suz" isabet kiyaslanacak; fark KANITLANIRSA
# davranis degisikligi v2'de gelir (Faz-1 ilkesi: kanitsiz davranis degisikligi YOK).

def _fvg_bul(mumlar):
    """v8 G1 — Fair Value Gap: son 3 ARDISIK KAPALI 15dk mum. SADECE KAYIT.
      Bullish: mum1.high < mum3.low -> bosluk [mum1.high, mum3.low]
      Bearish: mum1.low  > mum3.high -> bosluk [mum3.high, mum1.low]
    Donus: {'var','tur'('BULL'|'BEAR'|None),'aralik'([a,b]|None)}.
    3 mum yoksa veya son 3 mum ARDISIK degilse (veri boslugu) var=None — olculemedi,
    "bosluk yok" uydurulmaz (sifir tuzagi)."""
    ms = mumlar or []
    if len(ms) < 3:
        return {'var': None, 'tur': None, 'aralik': None}
    m1, m3 = ms[-3], ms[-1]
    if (m3.get('t') or 0) - (m1.get('t') or 0) != 1800:
        return {'var': None, 'tur': None, 'aralik': None}   # ardisik degil (mum kacmis)
    if m1['h'] > 0 and m3['l'] > 0 and m1['h'] < m3['l']:
        return {'var': True, 'tur': 'BULL', 'aralik': [round(m1['h'], 1), round(m3['l'], 1)]}
    if m1['l'] > 0 and m3['h'] > 0 and m1['l'] > m3['h']:
        return {'var': True, 'tur': 'BEAR', 'aralik': [round(m3['h'], 1), round(m1['l'], 1)]}
    return {'var': False, 'tur': None, 'aralik': None}


def _choch_bul(mumlar, sweep_ts):
    """v8 G2 — CHoCH: sweep'ten SONRAKI ilk TERS yapi kirilimi. SADECE KAYIT.
    Yapi, sweep ONCESI son 6 pivottan (3-mum kurali) cikar: HH+HL = YUKARI,
    LH+LL = ASAGI. Belirlenemiyorsa var=None (sifir tuzagi: uydurma yok).
    ASAGI yapida sonraki bir mumun high'i son swing-high'i asarsa (higher-high)
    CHoCH; YUKARI yapida simetrik (low, son swing-low altina).
    Donus: {'var': True/False/None, 'gecikme_mum': int|None, 'yapi': str|None}
    — var=False 'henuz yok' demektir; kesinlestirme cagiranin isidir (CHOCH_MAX_MUM)."""
    ms = [m for m in (mumlar or []) if m.get('t') is not None]
    onceki = [m for m in ms if m['t'] <= sweep_ts]
    sonraki = sorted((m for m in ms if m['t'] > sweep_ts), key=lambda m: m['t'])
    son6 = _kline_pivotlar(onceki)[-6:]
    hs = [p['fiyat'] for p in son6 if p['tur'] == 'H']
    ls = [p['fiyat'] for p in son6 if p['tur'] == 'L']
    if len(hs) < 2 or len(ls) < 2:
        return {'var': None, 'gecikme_mum': None, 'yapi': None}
    yukari = hs[-1] > hs[0] and ls[-1] > ls[0]     # HH + HL
    asagi = hs[-1] < hs[0] and ls[-1] < ls[0]      # LH + LL
    if yukari == asagi:                            # ne o ne bu -> yapi belirsiz
        return {'var': None, 'gecikme_mum': None, 'yapi': None}
    yapi = 'YUKARI' if yukari else 'ASAGI'
    for i, m in enumerate(sonraki):
        if yapi == 'ASAGI' and m['h'] > hs[-1]:
            return {'var': True, 'gecikme_mum': i + 1, 'yapi': yapi}
        if yapi == 'YUKARI' and m['l'] < ls[-1]:
            return {'var': True, 'gecikme_mum': i + 1, 'yapi': yapi}
    return {'var': False, 'gecikme_mum': None, 'yapi': yapi}


def _choch_olgunlastir(olaylar, mumlar, simdi_ts):
    """v8 G2 — bekleyen GRAB olaylarinin ham.choch'unu isler (kayit amacli).
    Olay ham.mum_ts'inden sonra ilk ters yapi kirilimi aranir; bulunursa veya
    CHOCH_MAX_MUM mum gecerse sonuc kesin=True ile MUHURLENIR (bir daha islenmez;
    kohort tekrar tekrar yazilmaz). olaylar YERINDE guncellenir; degisiklik
    olduysa True doner. SAF (I/O yok) — G2-2 testi dogrudan cagirir."""
    degisti = False
    for o in (olaylar or []):
        if not str(o.get('tetik', '')).startswith('GRAB'):
            continue
        h = o.get('ham')
        if not isinstance(h, dict):
            continue
        ch = h.get('choch')
        if isinstance(ch, dict) and ch.get('kesin'):
            continue
        sts = h.get('mum_ts')
        if not sts:
            continue
        yas_mum = int((simdi_ts - sts) // 900)
        if yas_mum < 1:
            continue                          # sweep mumunun kendisi — sonrasi henuz yok
        r = _choch_bul(mumlar, sts)
        if r['var'] is True and (r['gecikme_mum'] or 0) <= CHOCH_MAX_MUM:
            r['kesin'] = True
        elif yas_mum >= CHOCH_MAX_MUM:
            if r['var'] is True:              # pencere DISINDA bulundu -> pencere ici YOK
                r = {'var': False, 'gecikme_mum': None, 'yapi': r.get('yapi')}
            r['kesin'] = True                 # False/None neyse oyle muhurlenir
        else:
            r['kesin'] = False
        if ch != r:
            h['choch'] = r
            degisti = True
    return degisti


def _eq_kumeleri(pivotlar):
    """v8 G3 — EQUAL HIGHS/LOWS: ayni tur (H/H veya L/L) iki+ pivot birbirine
    <= SWING_EQ_BANT (fiyat orani) yakinsa EQ kumesi — en yogun stop kumeleri,
    balinanin ilk hedefi. Donus: [{'fiyat'(medyan),'tur'('EQH'|'EQL'),'n'}]."""
    out = []
    for tur, etiket in (('H', 'EQH'), ('L', 'EQL')):
        fs = sorted(p['fiyat'] for p in (pivotlar or [])
                    if p.get('tur') == tur and p.get('fiyat') and p['fiyat'] > 0)
        kume = []
        def _kapat(k):
            if len(k) >= 2:
                out.append({'fiyat': round(k[len(k) // 2], 1), 'tur': etiket, 'n': len(k)})
        for f in fs:
            if kume and (f - kume[0]) / kume[0] <= SWING_EQ_BANT:
                kume.append(f)
            else:
                _kapat(kume)
                kume = [f]
        _kapat(kume)
    return out


def _kohort_buda(olaylar, azami):
    """
    v9.4 — Kohort budamasi GERCEK SINYALLERI korur. SAF fonksiyon.
    Eski budama son-N dilimiydi: gunde ~96 GRAB_ADAY/N1 teshis kaydi 500'luk
    pencereyi ~5 gunde tur attirip NADIR gercek sinyalleri siliyordu (canli
    kanit: 22 Tem GRAB_DONUS sinyali silindi — 30 Tem dumpinda kohortun 500
    kaydinin 500'u ADAY/N1 cikti, tek gercek sinyal kayipti).
    Kural: limit asilirsa once ADAY kayitlari (tetik 'GRAB_ADAY' ile baslar)
    ESKIDEN yeniye atilir; gercek sinyaller (kademe SINYAL + GRAB_DONUS/DEVAM)
    ancak tek baslarina limiti asarsa (teorik) en eskiden kesilir. Sira korunur.
    tasfiye_kohortu BU YOLU KULLANMAZ (orada aday seli yok — davranisi degismez).
    """
    if len(olaylar) <= azami:
        return olaylar
    fazla = len(olaylar) - azami
    atilan = 0
    yeni = []
    for o in olaylar:
        if atilan < fazla and str((o or {}).get('tetik') or '').startswith('GRAB_ADAY'):
            atilan += 1
            continue
        yeni.append(o)
    return yeni[-azami:]   # aday yetmediyse (teorik: sinyal>azami) en eskiden kes


def _swing_backtest(olaylar, fiyat_seri, ufuklar):
    """
    v8.2 — SWING kohortu geri-testi. SAF fonksiyon (deterministik; "simdi" fiyat
    serisinin son ts'i). Her SINYAL olayi icin, sinyal anindan ufuk sonuna kadar
    FIYAT YOLU: swing_hedef mi STOP mu ONCE vuruldu?
      win  -> +rr_swing R   |   loss -> -1 R   |   cozulmemis/erken -> ACIK (sayilmaz)
    Scalp geri-testinden AYRI; swing ufuklari (4s/12s/1g/3g). Skoru ETKILEMEZ.

    olaylar: [{'zaman'(iso),'yon','swing_hedef','stop','rr_swing'}, ...]
    fiyat_seri: [(epoch, fiyat), ...]  ufuklar: [(etiket, saniye), ...]
    Donus: {etiket: {'n','win','loss','acik','isabet'(%|None),'ort_r'(R|None)}}.
    """
    seri = sorted(((t, f) for t, f in (fiyat_seri or []) if f and f > 0), key=lambda x: x[0])
    simdi = seri[-1][0] if seri else 0
    cikti = {et: {'n': 0, 'win': 0, 'loss': 0, 'acik': 0, 'isabet': None, 'ort_r': None,
                  '_r': 0.0} for et, _ in ufuklar}
    if not seri:
        return {et: {k: v for k, v in b.items() if k != '_r'} for et, b in cikti.items()}
    for o in (olaylar or []):
        z = o.get('zaman', '')
        try:
            # v8.7: naive ISO (motor utcnow().isoformat() yazar, 'Z'siz) yerel saat
            # DEGIL UTC varsayilir — yoksa .timestamp() istemci TZ'sine gore kayar
            # (Istanbul'da 3 saat; kabul testi de yerelde cokuyordu — denetim bulgusu).
            _zd = datetime.datetime.fromisoformat(z.replace('Z', '+00:00')) if z else None
            if _zd is not None and _zd.tzinfo is None:
                _zd = _zd.replace(tzinfo=datetime.timezone.utc)
            ep = _zd.timestamp() if _zd else None
        except Exception:
            ep = None
        yon, hedef, stop, rr = o.get('yon'), o.get('swing_hedef'), o.get('stop'), o.get('rr_swing')
        if ep is None or yon not in ('LONG', 'SHORT') or not hedef or not stop or rr is None:
            continue
        for et, sn in ufuklar:
            box = cikti[et]
            ufuk_son = ep + sn
            pencere = [(t, f) for t, f in seri if ep < t <= ufuk_son]
            sonuc = None
            for t, f in pencere:
                if yon == 'LONG':
                    if f <= stop:
                        sonuc = 'loss'; break
                    if f >= hedef:
                        sonuc = 'win'; break
                else:
                    if f >= stop:
                        sonuc = 'loss'; break
                    if f <= hedef:
                        sonuc = 'win'; break
            if sonuc == 'win':
                box['n'] += 1; box['win'] += 1; box['_r'] += rr
            elif sonuc == 'loss':
                box['n'] += 1; box['loss'] += 1; box['_r'] += -1.0
            else:
                box['acik'] += 1        # ufuk dolmadi ya da ne hedef ne stop -> cozulmemis
    son = {}
    for et, box in cikti.items():
        tot = box['win'] + box['loss']
        son[et] = {'n': box['n'], 'win': box['win'], 'loss': box['loss'], 'acik': box['acik'],
                   'isabet': round(box['win'] / tot * 100, 1) if tot > 0 else None,
                   'ort_r': round(box['_r'] / tot, 2) if tot > 0 else None}
    return son


def likidasyon_yogunlugu(agg_hacim, esik_medyan):
    """v7.3 — yon-bazli likidasyon hacminin kendi adaptif medyanina orani.
    >= TASFIYE_DIKEN_CARPANI ise DIKEN (zorla kapatma oldu). Birimler tutarli:
    pay Coinalyze 5dk penceresi (agg_liq_long/short), payda ayni kolonlarin
    7 gunluk SIFIR-OLMAYAN medyani. (Spec'in duz medyani sifir-agirlikli
    kolonda 1.0'a tabanlanir ve HER dakikayi diken yapardi — duzeltildi.)"""
    if not esik_medyan or esik_medyan <= 0:
        return 0.0
    return (agg_hacim or 0.0) / esik_medyan


def _tasfiye_bayraklari(tasfiye_long_yog, tasfiye_short_yog, d_oi_pct):
    """
    v7.3.1 — kohort etiketi REJIM ADINDAN degil HAM OLGUDAN.
    Dip supurmesinin geri-alim barinda tasfiye edilenler LONG'lardir ama o barin
    OI-matris rejimi SHORT_SQUEEZE/TASFIYE_SONRASI_DONUS cikar; rejim-adina bagli
    etiketleme kanonik supurmeyi (1 Tem) 2x2'de YANLIS HUCREYE dusuruyor ve saf
    tasfiye olaylarini (orta fiyat bandi) tamamen kaciriyordu.
    Donus: (tasfiye_var, tasfiye_yonu) — yon 'LONG'/'SHORT'/'IKISI'/None.
    Test edilebilir olsun diye ayri fonksiyon (G1b bunu dogrudan cagirir).
    """
    zl = tasfiye_long_yog >= TASFIYE_DIKEN_CARPANI
    zs = tasfiye_short_yog >= TASFIYE_DIKEN_CARPANI
    oi_dustu = d_oi_pct <= -TASFIYE_OI_MIN_PCT
    tasfiye_var = (zl or zs) and oi_dustu
    yon = ('IKISI' if (zl and zs) else 'LONG' if zl else 'SHORT' if zs else None)
    return tasfiye_var, yon


def adaptif_esik_guncelle():
    time.sleep(30)
    while True:
        try:
            simdi = time.time()
            yedi_gun_once = datetime.datetime.utcnow() - datetime.timedelta(days=7)
            bir_gun_once = datetime.datetime.utcnow() - datetime.timedelta(days=1)

            # v7.3: anlik_fiyat (volatilite birimi + seviye tespiti) ve yon-bazli
            # likidasyon kolonlari EKLENDI (sorgu zaten atiliyor — ek REST yok).
            res = (supabase.table("balina_avcisi_data")
                   .select("kayit_zamani,anlik_fiyat,order_book_depth_bid_1pct,"
                           "liquidation_pool_volume,vadeli_cvd,spot_cvd,"  # v7.4: spot_cvd
                           "agg_long_liq,agg_short_liq")
                   .gte("kayit_zamani", yedi_gun_once.isoformat())
                   .order("kayit_zamani", desc=True)
                   .limit(20000)
                   .execute())
            veriler = res.data or []

            if len(veriler) < MIN_KAYIT_ADAPTIF:
                logging.info(f"Adaptif esik: yetersiz veri ({len(veriler)}<{MIN_KAYIT_ADAPTIF}).")
                time.sleep(ESIK_GUNCELLEME_ARALIGI)
                continue

            bir_gun_iso = bir_gun_once.isoformat()
            kisa_derinlik, uzun_derinlik = [], []
            kisa_likid, uzun_likid = [], []
            cvd_seri = []    # FIX1: (epoch, vadeli_cvd SEVIYE) — delta buradan turetilir
            spot_seri = []   # v7.4: (epoch, spot_cvd SEVIYE) — spot esigi buradan
            fiyat_seri = []  # v7.3: (epoch, fiyat) — volatilite birimi + seviye tespiti
            lik_long_nz, lik_short_nz = [], []   # v7.3: yon-bazli SIFIR-OLMAYAN likidasyonlar

            for v in veriler:
                d = v.get('order_book_depth_bid_1pct')
                l = v.get('liquidation_pool_volume')
                c = v.get('vadeli_cvd')
                t = v.get('kayit_zamani', '')
                kisa_mi = t >= bir_gun_iso
                if d is not None:
                    uzun_derinlik.append(float(d))
                    if kisa_mi:
                        kisa_derinlik.append(float(d))
                if l is not None:
                    uzun_likid.append(float(l))
                    if kisa_mi:
                        kisa_likid.append(float(l))
                # Satir-bazli try: tek bozuk satir TUM kalibrasyon turunu oldurmesin
                # (float() donusumleri disarida kalsaydi dis except'e duserdi).
                try:
                    ep = None
                    if t:
                        ep = datetime.datetime.fromisoformat(
                            t.replace('Z', '+00:00')).timestamp()
                    if ep is not None:
                        if c is not None:
                            cvd_seri.append((ep, float(c)))
                        sc = v.get('spot_cvd')
                        if sc is not None:
                            spot_seri.append((ep, float(sc)))   # v7.4
                        f = v.get('anlik_fiyat')
                        if f is not None and float(f) > 0:
                            fiyat_seri.append((ep, float(f)))
                    ll = float(v.get('agg_long_liq') or 0)
                    ls_ = float(v.get('agg_short_liq') or 0)
                    if ll > 0:
                        lik_long_nz.append(ll)
                    if ls_ > 0:
                        lik_short_nz.append(ls_)
                except Exception:
                    continue

            kisa_derinlik = _aykiri_degerleri_temizle(kisa_derinlik)
            uzun_derinlik = _aykiri_degerleri_temizle(uzun_derinlik)
            kisa_likid = _aykiri_degerleri_temizle(kisa_likid)
            uzun_likid = _aykiri_degerleri_temizle(uzun_likid)

            d_kisa = _yuzdelik(kisa_derinlik, 0.90)
            d_uzun = _yuzdelik(uzun_derinlik, 0.90)
            yeni_derinlik = max([x for x in [d_kisa, d_uzun] if x is not None], default=None)

            kisa_likid_nz = [x for x in kisa_likid if x > 0]
            uzun_likid_nz = [x for x in uzun_likid if x > 0]
            l_kisa = _yuzdelik(kisa_likid_nz, 0.75)
            l_uzun = _yuzdelik(uzun_likid_nz, 0.75)
            yeni_likid = max([x for x in [l_kisa, l_uzun] if x is not None], default=None)

            # FIX1: CVD esigi SEVIYE degil 5-dk DELTA dagilimindan (canli kodla ayni
            # pencere). Boylece satis/alis_yogunlugu 0..1 araligini gercekten kullanir.
            cvd_deltalar = _aykiri_degerleri_temizle(
                _cvd_delta_serisi(cvd_seri, PENCERE_DK * 60))
            yeni_cvd_neg = None
            yeni_cvd_poz = None
            if len(cvd_deltalar) >= MIN_KAYIT_ADAPTIF:
                yeni_cvd_neg = _yuzdelik(cvd_deltalar, 0.10)
                yeni_cvd_poz = _yuzdelik(cvd_deltalar, 0.90)
                # Payda cokmesine (ust=0 -> _norm saturasyonu -> gurultuye ates) karsi
                # taban: esas olarak veriye gorecelidir (0.5*median|dL|); mutlak taban
                # yalnizca olu-piyasa guvenlik agi. Taban-yuksek yalnizca olu piyasada
                # bastirir (dogru yon); aktif piyasada percentil zaten ustundedir.
                mutlak = sorted(abs(x) for x in cvd_deltalar)
                medyan_abs = _yuzdelik(mutlak, 0.50) or 0.0
                cvd_taban = max(0.5 * medyan_abs, CVD_ESIK_MUTLAK_TABAN)
                if yeni_cvd_neg is not None:
                    yeni_cvd_neg = min(yeni_cvd_neg, -cvd_taban)
                if yeni_cvd_poz is not None:
                    yeni_cvd_poz = max(yeni_cvd_poz, cvd_taban)
            else:
                # Yeterli delta ornegi yok -> CVD esigi DEGISMEZ (varsayilan/eski
                # korunur). Gozlemlenebilirlik: CVD katmani seviye-olcekli varsayilana
                # takili kalirsa (bootstrap/degrade) burada gorunur.
                logging.warning(
                    f"Adaptif CVD esigi guncellenmedi: yetersiz delta ornegi "
                    f"({len(cvd_deltalar)}<{MIN_KAYIT_ADAPTIF}); eski/varsayilan korunuyor.")

            # ============ v7.3: YENI ADAPTIF BIRIMLER + LIKIDITE SEVIYELERI ============
            # VOLATILITE BIRIMI: 5dk fiyat degisimlerinin medyan mutlak %'si.
            # Tum yapisal esikler (delme/yakinlik/kumeleme) bunun KATSAYISIDIR —
            # sabit yuzde sistemi tek volatilite rejimine hapsederdi.
            fiyat_pctler = _fiyat_pct_serisi(fiyat_seri, PENCERE_DK * 60)
            yeni_vol = None
            if len(fiyat_pctler) >= MIN_KAYIT_ADAPTIF:
                yeni_vol = max(_yuzdelik(sorted(fiyat_pctler), 0.50) or 0.0, 0.02)
            # LIKIDASYON TABANI: yon-bazli, SIFIR-OLMAYAN medyan (diken referansi).
            yeni_lik_long = _yuzdelik(sorted(lik_long_nz), 0.50) if len(lik_long_nz) >= 20 else None
            yeni_lik_short = _yuzdelik(sorted(lik_short_nz), 0.50) if len(lik_short_nz) >= 20 else None
            # v7.4: SPOT CVD ADAPTIF ESIGI (vadeli icin zaten var, spot icin YOKTU).
            # Spot ve vadeli AYNI olcekte degil -> ayri P10/P90 (ortak esik spot'u
            # sistematik baskin gosterirdi). Delta-tabanli, FIX1'le ayni mantik.
            spot_deltalar = _aykiri_degerleri_temizle(
                _cvd_delta_serisi(spot_seri, PENCERE_DK * 60))
            yeni_spot_neg = None
            yeni_spot_poz = None
            if len(spot_deltalar) >= MIN_KAYIT_ADAPTIF:
                ssort = sorted(spot_deltalar)
                sp10, sp90 = _yuzdelik(ssort, 0.10), _yuzdelik(ssort, 0.90)
                smabs = _yuzdelik(sorted(abs(x) for x in spot_deltalar), 0.50) or 0.0
                s_taban = max(0.5 * smabs, CVD_ESIK_MUTLAK_TABAN)
                yeni_spot_neg = -max(abs(sp10), s_taban) if (sp10 and sp10 < 0) else -s_taban
                yeni_spot_poz = max(sp90, s_taban) if (sp90 and sp90 > 0) else s_taban
            # LIKIDITE SEVIYELERI: 24s swing dip/tepe (60dk koruma, vol-kumeleme).
            vol_seviye = yeni_vol if yeni_vol else durum.esik_volatilite
            yeni_dipler = _likidite_seviyeleri_bul(fiyat_seri, vol_seviye, tepe_mi=False) \
                if SUPURME_TESPIT_AKTIF else []
            yeni_tepeler = _likidite_seviyeleri_bul(fiyat_seri, vol_seviye, tepe_mi=True) \
                if SUPURME_TESPIT_AKTIF else []

            with durum.lock:
                if yeni_derinlik and yeni_derinlik > 0:
                    durum.esik_derinlik = yeni_derinlik
                if yeni_likid and yeni_likid > 0:
                    durum.esik_likidasyon = yeni_likid
                if yeni_cvd_neg is not None and yeni_cvd_neg < 0:
                    durum.esik_cvd_negatif = yeni_cvd_neg
                if yeni_cvd_poz is not None and yeni_cvd_poz > 0:
                    durum.esik_cvd_pozitif = yeni_cvd_poz
                if yeni_vol is not None:
                    durum.esik_volatilite = yeni_vol
                if yeni_lik_long is not None and yeni_lik_long > 0:
                    durum.esik_lik_long_medyan = max(yeni_lik_long, 1.0)
                if yeni_lik_short is not None and yeni_lik_short > 0:
                    durum.esik_lik_short_medyan = max(yeni_lik_short, 1.0)
                if yeni_spot_neg is not None and yeni_spot_neg < 0:
                    durum.esik_spot_negatif = yeni_spot_neg   # v7.4
                if yeni_spot_poz is not None and yeni_spot_poz > 0:
                    durum.esik_spot_pozitif = yeni_spot_poz
                durum.likidite_dipler = yeni_dipler
                durum.likidite_tepeler = yeni_tepeler
                durum.esik_guncelleme_zamani = simdi
                durum.esik_veri_sayisi = len(veriler)

            logging.info(
                f"ADAPTIF ESIK GUNCELLENDI ({len(veriler)} kayit, "
                f"{len(cvd_deltalar)} CVD-delta) -> "
                f"Derinlik: ${durum.esik_derinlik:,.0f} | Likid: ${durum.esik_likidasyon:,.0f} | "
                f"CVD-satis(dP10): {durum.esik_cvd_negatif:,.0f} | "
                f"CVD-alis(dP90): {durum.esik_cvd_pozitif:,.0f} | "
                f"Vol5dk: %{durum.esik_volatilite:.3f} | "
                f"LikMedyan L/S: ${durum.esik_lik_long_medyan:,.0f}/${durum.esik_lik_short_medyan:,.0f} | "
                f"SpotCVD-esik: {durum.esik_spot_negatif:,.0f}/{durum.esik_spot_pozitif:,.0f} | "
                f"Seviye: {len(yeni_dipler)} dip, {len(yeni_tepeler)} tepe"
            )

            # ============ v8.0: SWING SEVIYE HARITASI (scalp'tan AYRI) ============
            # KURAL: scalp skor yoluna DOKUNMAZ. Ayri anahtar (swing_seviyeler_oto),
            # ayri kod yolu. Burasi (adaptif_esik 10dk kadans) swing seviyeleri icin
            # ideal — dakikalik degismezler; 7 gunluk veri + fiyat_seri + likidasyon
            # ZATEN elde, ek REST yok. Kilit DISINDA (elle okuma + yazma ag I/O).
            # Elle girilen seviyeler oncelikli (A3). Hata akisi kesmez.
            if SWING_SEVIYE_AKTIF:
                try:
                    _af = float(veriler[0].get('anlik_fiyat') or 0) if veriler else 0.0
                    _liq = [(float(v['anlik_fiyat']), float(v.get('agg_long_liq') or 0),
                             float(v.get('agg_short_liq') or 0))
                            for v in veriler if v.get('anlik_fiyat')]
                    _elle_kayit = _ayarlar_oku('swing_seviyeler_elle')
                    _elle = []
                    if _elle_kayit and _elle_kayit.get('deger'):
                        _dg = _elle_kayit['deger']
                        _elle = _dg.get('seviyeler', []) if isinstance(_dg, dict) else _dg
                    # v8 ADIM 1: 1s/4s pivotlar cok-zaman cakismasi icin (coinalyze
                    # thread'i saatte bir yeniler; yoksa bos liste -> puan eklenmez)
                    with durum.lock:
                        _p1 = list(durum.pivotlar_1s)
                        _p4 = list(durum.pivotlar_4s)
                    _oto = _swing_seviye_haritasi(
                        _af, fiyat_seri, vol_seviye, yeni_dipler, yeni_tepeler, _liq, _elle,
                        pivotlar_1s=_p1, pivotlar_4s=_p4)
                    # v8.8-A: kalicilik izi — eski haritayla eslestir (ilk_gorulme_ts +
                    # yenileme_sayisi tasinir). SADECE KAYIT; hicbir filtre degismez.
                    _oto = _seviye_kalicilik(durum.swing_seviyeler, _oto,
                                             vol_seviye, time.time())
                    durum.swing_seviyeler = _oto      # v8.1: ozet dongusu (dk) bunu okur
                    _gorunur = [s for s in _oto if not s.get('gizli')]
                    _ayarlar_yaz('swing_seviyeler_oto', {
                        'guncelleme': datetime.datetime.utcnow().isoformat(),
                        'anlik_fiyat': round(_af, 1),
                        'vol_pct': round(vol_seviye, 4) if vol_seviye else None,
                        'seviye_sayisi': len(_gorunur),
                        'seviyeler': _oto})
                    logging.info(f"SWING SEVIYE HARITASI: {len(_gorunur)} gorunur "
                                 f"(+{len(_oto) - len(_gorunur)} gizli) seviye yazildi")
                except Exception as e:
                    logging.warning(f"swing seviye haritasi hatasi (akis devam eder): {e}")

                # v8.7: motor_sabitleri artik MOTORDAN yazilir. Panel (v3+v4) bu
                # anahtari okuyor ama HICBIR yerde yazilmiyordu (denetim bulgusu) —
                # DB'deki deger eski bir surumden kalma TOHUM; adaptif medyanlar
                # degistikce panelin flush/diken carpanlari sessizce bayatlıyordu.
                try:
                    _ayarlar_yaz('motor_sabitleri', {
                        'guncelleme': datetime.datetime.utcnow().isoformat(),
                        'TASFIYE_DIKEN_CARPANI': TASFIYE_DIKEN_CARPANI,
                        'TASFIYE_OI_MIN_PCT': TASFIYE_OI_MIN_PCT,
                        'esik_lik_long_medyan': round(durum.esik_lik_long_medyan, 1),
                        'esik_lik_short_medyan': round(durum.esik_lik_short_medyan, 1),
                        'esik_volatilite': round(durum.esik_volatilite, 4),
                        'esik_cvd_negatif': round(durum.esik_cvd_negatif, 1),
                        'esik_cvd_pozitif': round(durum.esik_cvd_pozitif, 1),
                    })
                except Exception as e:
                    logging.warning(f"motor_sabitleri yazma hatasi: {e}")

                # v8.2 FAZ C: SWING KOHORTU GERI-TESTI (scalp geri-testinden AYRI).
                # fiyat_seri (7g) burada ZATEN elde -> ek REST yok. Swing ufuklariyla
                # (4s/12s/1g/3g) SINYAL olaylarini degerlendir; istatistik ayarlara yazilir.
                try:
                    _kk = _ayarlar_oku('swing_kohortu')
                    _olylar = []
                    if _kk and _kk.get('deger'):
                        _olylar = (_kk['deger'].get('olaylar', [])
                                   if isinstance(_kk['deger'], dict) else [])
                    if _olylar:
                        _ist = _swing_backtest(_olylar, fiyat_seri, SWING_UFUKLAR)
                        _ayarlar_yaz('swing_kohort_istatistik', {
                            'guncelleme': datetime.datetime.utcnow().isoformat(),
                            'olay_sayisi': len(_olylar),
                            # v8: kohortta artik GRAB_ADAY kayitlari da birikir; panelin
                            # "kaydedilen SINYAL" sayaci sadece GERCEK sinyalleri saymali
                            # (yon+stop tasiyan olaylar — _swing_backtest'in isledikleri)
                            'sinyal_sayisi': sum(1 for o in _olylar
                                                 if o.get('yon') in ('LONG', 'SHORT')
                                                 and o.get('stop')),
                            'ufuklar': _ist})
                        logging.info(f"SWING GERI-TEST: {len(_olylar)} olay -> "
                                     + " | ".join(f"{et}:{b['isabet']}%/{b['ort_r']}R"
                                                  for et, b in _ist.items() if b['n']))
                except Exception as e:
                    logging.warning(f"swing geri-test hatasi (akis devam eder): {e}")
        except Exception as e:
            logging.warning(f"Adaptif esik hesaplama hatasi: {e}")
        time.sleep(ESIK_GUNCELLEME_ARALIGI)


# =========================================================================
# ==================  v2 YENİ: BAĞLAMSAL SKORLAMA BEYNİ  ==================
# =========================================================================
# Buradaki temel felsefe: balina KAZANIRSA şöyle kazanır — pasif limit
# emirleriyle, agresif akışı EMEREK. Bunu tespit etmek için tek bir anlık
# değere değil, son PENCERE_DK dakikadaki İLİŞKİYE bakılır:
#   "Agresif satış varken (CVD düşüyor) fiyat düşmüyor ve arkada olgun bir
#    alım duvarı bekliyorsa -> birileri sessizce topluyor."
# Her bileşen 0-1 arası normalize edilir; absorbsiyon üçünün GEOMETRİK
# ortalamasıdır (üçü de gerekli ama dereceli). Sonra bağlam çarpanları
# (OI rejimi, spot teyidi, borsa ayrışması, funding) skoru güçlendirir
# ya da zayıflatır. Böylece skor siyah-beyaz değil, bir GÜÇ ölçer olur.
# =========================================================================

def _norm(deger, dusuk, yuksek):
    """deger'i [dusuk, yuksek] araligina gore 0..1'e sikistirir (clamp)."""
    if yuksek == dusuk:
        return 0.0
    x = (deger - dusuk) / (yuksek - dusuk)
    return max(0.0, min(1.0, x))


def _olgunluk_carpani(yas_sn):
    """Emir yasi SPOOFING FILTRESI olarak: genc dev duvar sahte sayilir
    (carpani dusuk), olgun duvar gercek sayilir (carpani ~1).
    <60sn -> 0.30 (spoof suphesi), 300sn+ -> 1.0 (kurumsal iz)."""
    if yas_sn <= 60:
        return 0.30
    if yas_sn >= EMIR_OLGUNLUK_SANIYE:
        return 1.0
    # 60..300 arasi dogrusal 0.30 -> 1.0
    return 0.30 + 0.70 * (yas_sn - 60) / (EMIR_OLGUNLUK_SANIYE - 60)


def _cvd_iraksama_hesapla(d_vadeli, d_spot):
    """
    C maddesi — CVD IRAKSAMA sinyali.
    Spot ve vadeli CVD ayni yone mi bakiyor?
      +1.0'a yakin  -> tam TEYİT (ikisi de ayni yonde, guclu hareket)
       0.0'a yakin  -> notr / belirsiz
      -1.0'a yakin  -> IRAKSAMA (zit yonler; vadeli itiyor spot onaylamiyor
                       ya da tersi -> kaldiracli, kirilgan hareket)
    Buyukluk normalize edilir; sadece YON degil, uyum DERECESI olcuulur.
    """
    if d_vadeli == 0 and d_spot == 0:
        return 0.0
    # Isaret uyumu: ayni yon +, zit yon -
    isaret = 1.0 if (d_vadeli * d_spot) >= 0 else -1.0
    # Buyukluklerin ne kadar dengeli oldugu (biri cok kucukse uyum zayif sayilir)
    buyuk = max(abs(d_vadeli), abs(d_spot))
    kucuk = min(abs(d_vadeli), abs(d_spot))
    denge = (kucuk / buyuk) if buyuk > 0 else 0.0
    return isaret * denge


def veri_kalitesi_degerlendir(cvd_kaynak_saglikli, open_interest, anlik_fiyat,
                              son_guncelleme_gecen, funding):
    """
    A maddesi — VERİ KALİTE KAPISI.
    Her dakika, o anki verinin skorlamaya GÜVENİLİR olup olmadigini karar verir.
    Kotu veriyle sinyal uretmeyi tamamen engeller. Ham veri yine tabloya
    yazilir (analiz icin atmiyoruz) ama skor uretilmez.
    Donus: dict(cvd_guvenilir: bool, sebep: str)
    """
    # 1) CVD kaynagi: SADECE Coinalyze (cok-borsali, USD, gercek taker) guvenilir.
    #    WS bookTicker yedegi 10.000x olcek farkli + yanlis proxy -> reddet.
    if not cvd_kaynak_saglikli:
        return {"cvd_guvenilir": False, "sebep": "CVD yedek kaynak (Coinalyze yok)"}

    # 2) Bozuk OI okumasi (verideki 99347 gibi degerler) -> guvensiz dakika.
    if open_interest is None or open_interest < 1e9:
        return {"cvd_guvenilir": False, "sebep": f"Bozuk OI ({open_interest})"}

    # 3) Fiyat yoksa zaten yazilmayacak; burada da guvensiz say.
    if anlik_fiyat is None or anlik_fiyat <= 0:
        return {"cvd_guvenilir": False, "sebep": "Fiyat yok"}

    # 4) Bayat veri: WS akisi 90sn+ durduysa, stale veriyle sinyal uretme.
    if son_guncelleme_gecen > 90:
        return {"cvd_guvenilir": False, "sebep": f"Bayat veri ({son_guncelleme_gecen:.0f}sn)"}

    return {"cvd_guvenilir": True, "sebep": "OK"}


# =========================================================================
# v7.3 — SÜPÜRME DURUM MAKİNESİ
# =========================================================================
# Supurme bir anlik fotograf degil, zaman icinde gerceklesen bir OLAYDIR:
# fiyat bilinen bir likidite havuzuna yaklasir (SILAHLI), fitille deler
# (DELINDI), stop kaskadi + kapitulasyonla birlikte GERI ALINIRSA (ONAYLI)
# kurulum olusur. Cok derin delme = KIRILMA (OLU); geri alinmayan fitil de OLU.
# FAZ 1: makine sadece TESPIT eder ve kohorta yazar — skora dokunmaz.
def supurme_takip_et(durumlar, seviyeler, tepe_mi, anlik_fiyat, fitil_uc,
                     vol_pct, kapitulasyon_var, tasfiye_var, simdi):
    """
    durumlar: {seviye_key: durum_dict} (ozet thread'ine ozel; yerinde guncellenir)
    seviyeler: [{'fiyat','test','yas_dk'},...] (adaptif donguden)
    fitil_uc: dip icin tick_min(60), tepe icin tick_max(60) — GERCEK fitil
    Donus: (aktif_onayli_detay_veya_None, yeni_onaylananlar_listesi)
    Cagiran, kalite kapisi/veri eksikligi durumunda HIC cagirmamali
    (spec: gecis yapilmaz, mevcut durumlar korunur).
    """
    yeni_onaylar = []
    if not vol_pct or vol_pct <= 0 or anlik_fiyat <= 0:
        return None, yeni_onaylar
    v = vol_pct / 100.0
    _ILERI_SIRA = {'BEKLEME': 0, 'SILAHLI': 1, 'DELINDI': 2, 'ONAYLI': 3, 'OLU': 3}

    # Seviye eslestirme: adaptif yenilemede kume medyani hafif kayar; mevcut
    # durumu 1 x vol icindeki yeni seviyeye TASI ve YENIDEN ANAHTARLA.
    # (Onceki surum d['seviye']'yi guncelleyip anahtari eski birakiyordu ->
    # durum yetim kaliyor, ayni seviyeye taze BEKLEME ikizi dogup izlenen
    # supurme unutuluyordu; donmus SILAHLI yetimi taze_satis sayacini
    # sonsuza dek zehirliyordu. Dogrulayici simulasyonla kanitladi.)
    mevcut_keyler = {round(s['fiyat']) for s in seviyeler}
    for key in list(durumlar.keys()):
        d = durumlar.get(key)
        if d is None or key in mevcut_keyler and round(d['seviye']) == key:
            continue
        es = next((s['fiyat'] for s in seviyeler
                   if d['seviye'] > 0 and abs(s['fiyat'] - d['seviye']) / d['seviye']
                   <= SEVIYE_KUMELEME_VOL * v), None)
        if es is not None:
            yeni_key = round(es)
            d['seviye'] = es
            if yeni_key != key:
                mevcut = durumlar.get(yeni_key)
                # Cakismada ILERI durum kazanir (DELINDI, taze BEKLEME'ye ezdirilmez)
                if mevcut is None or _ILERI_SIRA[d['durum']] >= _ILERI_SIRA[mevcut['durum']]:
                    durumlar[yeni_key] = d
                del durumlar[key]
        elif d['durum'] not in ('ONAYLI', 'OLU'):
            del durumlar[key]   # seviye kayboldu, kurulum yokken durum da gitsin
    # Yetim temizligi: anahtar mevcut seviyelerden turetilemiyorsa ve kurulum/
    # cooldown penceresi de bittiyse at (sonsuz 'SILAHLI' zombisi kalmasin).
    for key in list(durumlar.keys()):
        d = durumlar[key]
        if key in mevcut_keyler:
            continue
        if d['durum'] in ('ONAYLI', 'OLU'):
            son_ts = max(d.get('onay_ts', 0), d.get('olu_ts', 0))
            if simdi - son_ts <= SUPURME_COOLDOWN_DK * 60:
                continue
        del durumlar[key]

    for s in seviyeler:
        key = round(s['fiyat'])
        if key not in durumlar:
            durumlar[key] = {'seviye': s['fiyat'], 'test': s['test'],
                             'durum': 'BEKLEME', 'fitil_uc': 0.0,
                             'delinme_ts': 0.0, 'onay_ts': 0.0, 'olu_ts': 0.0,
                             'kap_ts': 0.0, 'tas_ts': 0.0}
        d = durumlar[key]
        d['test'] = s['test']
        sev = d['seviye']
        if sev <= 0:
            continue

        # yon-bagimli yardimcilar (dip: asagi delme; tepe: yukari delme)
        if tepe_mi:
            yakin = anlik_fiyat >= sev * (1 - SUPURME_YAKINLIK_VOL * v)
            delindi = fitil_uc > 0 and fitil_uc > sev * (1 + SUPURME_MIN_DELME_VOL * v)
            delme_pct = ((fitil_uc / sev) - 1.0) * 100.0 if fitil_uc > 0 else 0.0
            geri_alim = anlik_fiyat <= sev
        else:
            yakin = anlik_fiyat <= sev * (1 + SUPURME_YAKINLIK_VOL * v)
            delindi = fitil_uc > 0 and fitil_uc < sev * (1 - SUPURME_MIN_DELME_VOL * v)
            delme_pct = (1.0 - (fitil_uc / sev)) * 100.0 if fitil_uc > 0 else 0.0
            geri_alim = anlik_fiyat >= sev

        st = d['durum']
        if st == 'OLU':
            if simdi - d['olu_ts'] > SUPURME_COOLDOWN_DK * 60:
                d['durum'] = 'BEKLEME'
                # yeni dongu temiz baslar: eski fitil/latch'ler tasinmaz
                d['fitil_uc'] = 0.0; d['delinme_ts'] = 0.0
                d['kap_ts'] = 0.0; d['tas_ts'] = 0.0
                st = d['durum']
        elif st == 'ONAYLI':
            if simdi - d['onay_ts'] > SUPURME_GECERLILIK_DK * 60:
                d['durum'] = 'OLU'          # gecerlilik bitti -> cooldown'a gec
                d['olu_ts'] = d['onay_ts']  # cooldown, onaydan itibaren sayilir

        # ============== v7.3.3 KRİTİK DÜZELTME: AYNI TURDA ZİNCİRLEME ==============
        # ONCEKI HATA: durumlar 'elif' zinciriyle baglilardi -> BEKLEME->SILAHLI
        # gecisi BIR TUR harciyordu ve delme kontrolu ancak BIR SONRAKI dongude
        # (60sn sonra) yapiliyordu. Ama supurme fitili SANIYELER surer:
        #   Tur T   : fiyat uzak            -> BEKLEME
        #   Tur T+1 : FITIL indi + geri alindi -> BEKLEME'den SILAHLI'ya gecti,
        #             ama 'delindi' o turda HIC KONTROL EDILMEDI
        #   Tur T+2 : fiyat normale dondu   -> delindi=False, fitil UNUTULDU
        # Sonuc: 61 saatlik gercek veride 9 supurme olayi OLMASI gerekirken
        # motor SIFIR kaydetti (dogrulandi: huni analizi, ham veri).
        # DUZELTME: durumlar ardisik 'if'lerle zincirlenir; bir tur icinde
        # BEKLEME -> SILAHLI -> DELINDI -> (geri alim varsa) ONAYLI yapilabilir.
        # Bu, makinenin dogru davranisi: fitil ve geri alim AYNI tick penceresinde
        # gorulebilir (tick_min/tick_max zaten saniyelik tick akisindan besleniyor).
        if st == 'BEKLEME' and yakin:
            d['durum'] = 'SILAHLI'
            st = 'SILAHLI'

        if st == 'SILAHLI':
            if delindi:
                d['durum'] = 'DELINDI'
                d['delinme_ts'] = simdi
                d['fitil_uc'] = fitil_uc
                # Teyit LATCH'leri delme aninda baslar (asagida aciklama)
                if kapitulasyon_var:
                    d['kap_ts'] = simdi
                if tasfiye_var:
                    d['tas_ts'] = simdi
                # KIRILMA kontrolu ANINDA: 8 x vol'den derin delme supurme degil,
                # kirilmadir — bir sonraki dakikayi bekleyip DELINDI'de oyalanmaz.
                if delme_pct > SUPURME_MAX_DELME_VOL * vol_pct:
                    d['durum'] = 'OLU'; d['olu_ts'] = simdi
                st = d['durum']
            elif not yakin:
                d['durum'] = 'BEKLEME'
                st = 'BEKLEME'

        if st == 'DELINDI':
            # Teyitler LATCH'lenir: kapitulasyon ve tasfiye dikeni FITILDE gorulur
            # (spec §1 tablosu), geri alim ise 6-15 dk SONRA gelebilir — o anda
            # 5-dk pencereler coktan sonmustur. Ayni-dakika kosulu, kanonik
            # (1 Tem tarzi) supurmeyi SISTEMATIK kacirirdi; latch bunu duzeltir.
            # Hangi dakikada latch'lendigi kohort ham metriklerine yazilir.
            if kapitulasyon_var and not d.get('kap_ts'):
                d['kap_ts'] = simdi
            if tasfiye_var and not d.get('tas_ts'):
                d['tas_ts'] = simdi
            # fitil ucunu guncelle (en asiri nokta)
            if fitil_uc > 0:
                d['fitil_uc'] = max(d['fitil_uc'], fitil_uc) if tepe_mi \
                    else (min(d['fitil_uc'], fitil_uc) if d['fitil_uc'] > 0 else fitil_uc)
            asiri = ((d['fitil_uc'] / sev - 1.0) if tepe_mi
                     else (1.0 - d['fitil_uc'] / sev)) * 100.0 if d['fitil_uc'] > 0 else 0.0
            if asiri > SUPURME_MAX_DELME_VOL * vol_pct:
                d['durum'] = 'OLU'; d['olu_ts'] = simdi       # KIRILMA, supurme degil
            elif simdi - d['delinme_ts'] > SUPURME_GERI_ALIM_MAX_DK * 60:
                d['durum'] = 'OLU'; d['olu_ts'] = simdi       # geri alim gelmedi
            elif geri_alim and d.get('kap_ts') and d.get('tas_ts'):
                # ZORUNLU teyitler (spec §7.3): geri alim (kanitin kendisi) +
                # kapitulasyon (fitilde agresif akis, latch) + tasfiye dikeni
                # (stoplar GERCEKTEN toplandi, latch). Spot ve OI kapi DEGIL —
                # 1 Tem kaniti: spot 10 saat gec geldi; kohortta GECIKMELI olculur.
                d['durum'] = 'ONAYLI'
                d['onay_ts'] = simdi
                yeni_onaylar.append({'seviye': sev, 'test': d['test'],
                                     'fitil_uc': d['fitil_uc'],
                                     'delme_pct': round(asiri, 4),
                                     'kap_gecikme_dk': round((d['kap_ts'] - d['delinme_ts']) / 60.0, 1),
                                     'tas_gecikme_dk': round((d['tas_ts'] - d['delinme_ts']) / 60.0, 1),
                                     'geri_alim_dk': round((simdi - d['delinme_ts']) / 60.0, 1)})

    # Aktif (gecerli) ONAYLI kurulum var mi? En tazesi doner.
    aktif = None
    for d in durumlar.values():
        if d['durum'] == 'ONAYLI' and (simdi - d['onay_ts']) <= SUPURME_GECERLILIK_DK * 60:
            if aktif is None or d['onay_ts'] > aktif['onay_ts']:
                aktif = d
    return aktif, yeni_onaylar


def surec_takip_et(durum_ref, rejim, anlik_fiyat, spot_cvd, vadeli_cvd,
                   bid_d, ask_d, bid_yas, ask_yas, funding, agg_liq_long,
                   agg_liq_short, seri):
    """
    v4 — SÜREÇ HAFIZASI.
    Anlik rejim (DIP_TOPLAMA / TEPE_DAGITIM / SHORT_SQUEEZE / LONG_TASFIYE)
    saatlerce surebilir. Bu fonksiyon o surecin:
      - kac dakikadir surdugunu,
      - ne kadar olgunlastigini (0-1),
      - TUKENME sinyallerinin belirip belirmedigini (0-4)
    izler. "Bitis" tahmini, tukenme sinyallerinin birikmesiyle olasilik
    olarak verilir (KESIN degil — hicbir sistem tepeyi kesin bilemez).

    Donus: dict(surec_rejim, sure_dk, olgunluk, tukenme, tukenme_detay, uyari)
    """
    simdi = time.time()
    # Dagitim/toplama "yonlu surec" sayilir; digerleri surec baslatmaz
    # v7.3: SHORT_TASFIYE = SHORT_SQUEEZE'in, LONG_KAPITULASYON = LONG_TASFIYE'nin
    # zenginlestirilmis adi. AILE DAVRANISLARI BIREBIR AYNI — degilse Faz 1'in
    # "davranis degismez" garantisi bozulur (rejim -> surec -> VE-kapisi akisi).
    # v7.3.1: TASFIYE_SONRASI_DONUS eklendi. DIKKAT — duzeltme talimati yalnizca
    # _DAGITIM_SETI + dagitim_tarafi diyordu; 'yonlu' ve ayni_aile ANAHTARI
    # eklenmezse yeni rejim NOTR sayilir, surec sifirlanir ve v7.2'de ayni barin
    # surec baslattigi durumda davranis SAPARDI (talimatin kendi KABUL #1 ihlali).
    # v7.4: DIP_TOPLAMA_{SPOT,TEYITSIZ,PERP} = DIP_TOPLAMA'nin emilim-zenginlestirilmis
    # adlari. FAZ 1'de es-aile (davranis birebir); spec §5'in NO-OP mayini uyarisi.
    # v7.6: TEPE_DAGITIM_{SPOT,TEYITSIZ,PERP} = DIP_TOPLAMA_*'in dagitim simetrigi.
    yonlu = rejim in ("TEPE_DAGITIM", "DIP_TOPLAMA", "SHORT_SQUEEZE", "TAZE_ALIM",
                      "TAZE_SATIS", "LONG_TASFIYE", "SHORT_TASFIYE",
                      "LONG_KAPITULASYON", "TASFIYE_SONRASI_DONUS",
                      "DIP_TOPLAMA_SPOT", "DIP_TOPLAMA_TEYITSIZ", "DIP_TOPLAMA_PERP",
                      "TEPE_DAGITIM_SPOT", "TEPE_DAGITIM_TEYITSIZ", "TEPE_DAGITIM_PERP")

    # Surec devami mi, yeni surec mi?
    # v8: kume tanimlari SABITLER'e tasindi (DAGITIM_AILESI/TOPLAMA_AILESI — tek tanim,
    # GK-4). Degerler birebir ayni; asagidaki ayni_aile haritasi degismedi.
    _DAGITIM_SETI = DAGITIM_AILESI
    _TOPLAMA_SETI = TOPLAMA_AILESI
    ayni_aile = {
        "TEPE_DAGITIM": _DAGITIM_SETI,       # dagitim + squeeze/tasfiye = ayni hikaye
        "SHORT_SQUEEZE": _DAGITIM_SETI,
        "SHORT_TASFIYE": _DAGITIM_SETI,      # v7.3: squeeze ile ayni aile
        "TASFIYE_SONRASI_DONUS": _DAGITIM_SETI,  # v7.3.1: squeeze es-ailesi
        "TEPE_DAGITIM_SPOT": _DAGITIM_SETI,      # v7.6: tepe-dagitim es-ailesi
        "TEPE_DAGITIM_TEYITSIZ": _DAGITIM_SETI,
        "TEPE_DAGITIM_PERP": _DAGITIM_SETI,
        "DIP_TOPLAMA": _TOPLAMA_SETI,
        "LONG_TASFIYE": _TOPLAMA_SETI,
        "LONG_KAPITULASYON": _TOPLAMA_SETI,  # v7.3: long-tasfiye ile ayni aile
        "DIP_TOPLAMA_SPOT": _TOPLAMA_SETI,       # v7.4: dip-toplama es-ailesi
        "DIP_TOPLAMA_TEYITSIZ": _TOPLAMA_SETI,
        "DIP_TOPLAMA_PERP": _TOPLAMA_SETI,
        "TAZE_ALIM": {"TAZE_ALIM"},
        "TAZE_SATIS": {"TAZE_SATIS"},
    }
    devam = (durum_ref.surec_rejim in ayni_aile.get(rejim, set())) if yonlu else False

    if yonlu and not devam:
        # YENİ surec basliyor
        durum_ref.surec_rejim = rejim
        durum_ref.surec_baslangic = simdi
        durum_ref.surec_baslangic_fiyat = anlik_fiyat
        durum_ref.surec_baslangic_spotcvd = spot_cvd
        durum_ref.surec_zirve_fiyat = anlik_fiyat
        durum_ref.surec_dip_fiyat = anlik_fiyat
        durum_ref.surec_tukenme = 0
        durum_ref.surec_olgunluk = 0.0
    elif not yonlu:
        # Notr/guvensiz -> surec yok
        durum_ref.surec_rejim = "NOTR"
        return {"surec_rejim": "NOTR", "sure_dk": 0, "olgunluk": 0.0,
                "tukenme": 0, "tukenme_detay": [], "uyari": ""}

    # Zirve/dip guncelle
    durum_ref.surec_zirve_fiyat = max(durum_ref.surec_zirve_fiyat, anlik_fiyat)
    durum_ref.surec_dip_fiyat = min(durum_ref.surec_dip_fiyat, anlik_fiyat)

    sure_dk = (simdi - durum_ref.surec_baslangic) / 60.0

    # ============ TÜKENME SİNYALLERİ (dagitim/toplama icin) ============
    # Bir surecin BITISI, motorunun tukenmesiyle gelir. 4 klasik imza:
    tukenme_detay = []
    dagitim_tarafi = durum_ref.surec_rejim in ("TEPE_DAGITIM", "SHORT_SQUEEZE", "TAZE_ALIM")
    if not TASFIYE_AYRIMI_AKTIF:
        # FAZ 1: tasfiye rejimleri es-aile -> davranis v7.2 ile BIREBIR.
        # FAZ 2: aileden cikar (tukenme sayaci kapiya gidiyor; bkz. asagida
        # dagitim_ailesi'ndeki NO-OP aciklamasi).
        dagitim_tarafi = dagitim_tarafi or durum_ref.surec_rejim in (
            "SHORT_TASFIYE", "TASFIYE_SONRASI_DONUS")
    if not EMILIM_AYRIMI_AKTIF:
        # FAZ 1: TEPE_DAGITIM_* dagitim tarafinda (v7.5 ile BIREBIR tukenme imzasi).
        dagitim_tarafi = dagitim_tarafi or durum_ref.surec_rejim in (
            "TEPE_DAGITIM_SPOT", "TEPE_DAGITIM_TEYITSIZ", "TEPE_DAGITIM_PERP")

    # Pencere serisinden son ~20dk egilimleri
    def seri_deger(alan, geri=20):
        if len(seri) < 2:
            return None, None
        son = seri[-1].get(alan)
        idx = max(0, len(seri) - geri)
        eski = seri[idx].get(alan)
        return son, eski

    if dagitim_tarafi:
        # 1) Fiyat artik yukselemiyor (zirveye yaklasti ama momentum bitti)
        zirveden_geri = (durum_ref.surec_zirve_fiyat - anlik_fiyat) / durum_ref.surec_zirve_fiyat if durum_ref.surec_zirve_fiyat > 0 else 0
        if zirveden_geri > 0.001:  # zirveden %0.1+ geri geldi
            tukenme_detay.append("Fiyat zirveden geri cekiliyor")
        # 2) Satim duvari inceliyor (balinanin isi bitti, duvara gerek yok)
        ask_son, ask_eski = seri_deger('ask_d')
        if ask_son is not None and ask_eski and ask_son < ask_eski * 0.7:
            tukenme_detay.append("Satim duvari inceliyor")
        # 3) Funding asiri pozitif (kalabalik long doruga ulasti)
        if funding > 0.0004:
            tukenme_detay.append("Funding asiri pozitif (kalabalik long)")
        # 4) Ilk buyuk long likidasyonu (cig baslangici)
        if agg_liq_long > agg_liq_short * 1.5 and agg_liq_long > 50000:
            tukenme_detay.append("Long likidasyonlari basladi")
    else:
        # TOPLAMA tarafi tukenme (dip bitisi = yukselis baslangici)
        dipten_yukari = (anlik_fiyat - durum_ref.surec_dip_fiyat) / durum_ref.surec_dip_fiyat if durum_ref.surec_dip_fiyat > 0 else 0
        if dipten_yukari > 0.001:
            tukenme_detay.append("Fiyat dipten toparliyor")
        bid_son, bid_eski = seri_deger('bid_d')
        if bid_son is not None and bid_eski and bid_son < bid_eski * 0.7:
            tukenme_detay.append("Alim duvari cekiliyor (is bitti)")
        if funding < -0.0002:
            tukenme_detay.append("Funding negatif (kalabalik short)")
        if agg_liq_short > agg_liq_long * 1.5 and agg_liq_short > 50000:
            tukenme_detay.append("Short likidasyonlari basladi")

    tukenme = len(tukenme_detay)
    durum_ref.surec_tukenme = tukenme

    # ============ OLGUNLUK (0-1) ============
    # Sure + tukenme birlikte olgunlugu belirler. ~90dk ve/veya 3+ tukenme
    # sinyali = surec olgun, bitis yakin olabilir.
    sure_faktor = min(1.0, sure_dk / 90.0)
    tukenme_faktor = min(1.0, tukenme / 3.0)
    olgunluk = 0.5 * sure_faktor + 0.5 * tukenme_faktor
    durum_ref.surec_olgunluk = olgunluk

    # ============ UYARI METNİ ============
    uyari = ""
    if olgunluk >= 0.75 and tukenme >= 2:
        if dagitim_tarafi:
            uyari = "DAGITIM OLGUN — tukenme sinyalleri birikti, dususe donus riski YUKSEK"
        else:
            uyari = "TOPLAMA OLGUN — tukenme sinyalleri birikti, yukselise donus olasi"
    elif olgunluk >= 0.5:
        uyari = "Surec olgunlasiyor — izlemede kal"

    return {"surec_rejim": durum_ref.surec_rejim, "sure_dk": round(sure_dk, 1),
            "olgunluk": round(olgunluk, 2), "tukenme": tukenme,
            "tukenme_detay": tukenme_detay, "uyari": uyari}


def balina_skoru_hesapla(a, pencere, kalite):
    """
    v3 — KATMAN HİYERARŞİSİ ile skorlama.

    Order flow katmanlari EŞİT GÜVENİLİR DEĞİL. Hiyerarsi:
      Katman 2 (İŞLEMLER/CVD) -> EN GÜVENİLİR. Cekirdek belirleyici.
      Katman 3 (ABSORBSİYON)  -> duvar<->islem iliskisi. Turetilmis.
      Katman 1 (DUVAR)        -> SPOOF'A ACIK. Tek basina sinyal ÜRETEMEZ;
                                 ancak islem tarafindan TEYİT edilirse guc katar.
      Katman 4 (OI/FUNDING)   -> KIRILGANLIK VETOSU (squeeze'de sinyali keser).

    a: guncel degerler dict'i
    pencere: PENCERE_DK once ile simdi arasi degisimler (yoksa None)
    kalite: dict(cvd_guvenilir: bool, sebep: str) -- A maddesi
    Donus: (long_skor, short_skor, sinyal, rejim, aciklama, emilim,
            golge_yon, golge_kapi, golge_skor)  # v7.4: 6. eleman; v9.3: 7-9 (golge)
    """
    # v7.4: erken-donuslerde de emilim (bos) dondur — cagiran artik 9 eleman ACIYOR
    # (v9.3). Erken donusler de 9'lu olmali: skor 0'da golge tanim geregi None
    # (esik alti) — ayni None'lar burada da doner, arity ASLA degismez.
    _bos_emilim = {'emilim_esnekligi': None, 'emilim_borsasi': None,
                   'emilim_spot_pay': None, 'satici_tukenmesi': False,
                   'sonme_orani': None, 'esik_spot_neg': round(a.get('esik_spot_neg', -2000.0), 0)}
    # ---- A) VERİ KALİTE KAPISI: kotu veriyle ASLA skor uretme ----
    if not kalite['cvd_guvenilir']:
        return (0.0, 0.0, "BEKLE", "VERI_GUVENSIZ", f"Kalite reddi: {kalite['sebep']}",
                _bos_emilim, None, None, None)
    if pencere is None:
        return (0.0, 0.0, "BEKLE", "VERI_BEKLENIYOR", "Pencere dolmadi",
                _bos_emilim, None, None, None)

    d_fiyat = pencere['d_fiyat_pct']       # % fiyat degisimi (pencere)
    d_vadeli = pencere['d_vadeli_cvd']     # vadeli CVD degisimi (KATMAN 2)
    d_spot = pencere['d_spot_cvd']         # spot CVD degisimi (KATMAN 2)
    d_oi = pencere['d_oi_pct']             # % OI degisimi (KATMAN 4)

    esik_c_poz = max(a['esik_c_poz'], 1.0)
    esik_c_neg_abs = max(abs(a['esik_c_neg']), 1.0)

    # =====================================================================
    # KATMAN 2 — İŞLEMLER (CVD). En guvenilir. ÇEKİRDEK.
    # =====================================================================
    # Bu, skorun BELKEMİĞİ. Islem tarafinda gercek bir akis yoksa,
    # duvar ne kadar buyuk olursa olsun skor yukselemez.
    satis_yogunlugu = _norm(-d_vadeli, 0.0, esik_c_neg_abs * 1.5)   # LONG icin
    alis_yogunlugu = _norm(d_vadeli, 0.0, esik_c_poz * 1.5)         # SHORT icin

    # =====================================================================
    # KATMAN 3 — ABSORBSİYON (duvar<->islem iliskisi). Fiyat direnci.
    # =====================================================================
    fiyat_direnci_long = _norm(d_fiyat, -0.20, 0.10)   # satisa ragmen fiyat tuttu mu
    fiyat_zayifligi_short = _norm(-d_fiyat, -0.20, 0.10)  # alima ragmen fiyat dustu mu

    # =====================================================================
    # KATMAN 1 — DUVAR. SPOOF'A ACIK. Tek basina degersiz; TEYİT gerekli.
    # =====================================================================
    # Duvar buyuklugu x olgunluk (spoof filtresi).
    bid_buyukluk = _norm(a['bid_d'], a['esik_d'] * 0.5, a['esik_d'] * 1.5)
    ask_buyukluk = _norm(a['ask_d'], a['esik_d'] * 0.5, a['esik_d'] * 1.5)
    bid_olgunluk = _olgunluk_carpani(a['bid_yas'])
    ask_olgunluk = _olgunluk_carpani(a['ask_yas'])
    duvar_ham_long = bid_buyukluk * bid_olgunluk
    duvar_ham_short = ask_buyukluk * ask_olgunluk

    # DUVAR VETOSU: duvar, ISLEM tarafindan dogrulanmadikca guc KATMAZ.
    # LONG icin: duvar ancak o an gercek SATIS akisi varsa (satis_yogunlugu>0)
    # "emilen duvar" olur. Satis yoksa duvar sadece bekleyen bir spoof olabilir.
    # D EK: Duvar sadece TEK borsada gorunuyorsa (aktif_borsa<2) spoofing
    #       ihtimali yuksek -> teyit esigini yukselt (0.15 yerine 0.25).
    aktif_borsa = a.get('aktif_borsa', 2)
    teyit_esigi = 0.15 if aktif_borsa >= 2 else 0.25
    if satis_yogunlugu < teyit_esigi:
        duvar_teyitli_long = 0.0    # islem teyidi yok -> duvar NOTR
    else:
        duvar_teyitli_long = duvar_ham_long
    if alis_yogunlugu < teyit_esigi:
        duvar_teyitli_short = 0.0
    else:
        duvar_teyitli_short = duvar_ham_short

    # =====================================================================
    # ÇEKİRDEK ABSORBSİYON — AGIRLIKLI (islem BASKIN, duvar teyit)
    # =====================================================================
    # v2'de esit uslu geometrik ortalamaydi. v3'te islem katmanina daha
    # yuksek us (0.50), direnc ve duvara dusuk us (0.25). Boylece skoru
    # ISLEMLER belirler; duvar sadece teyit/carpan rolunde.
    # Duvar teyitsizse (0.0) carpani 1.0 yaparak absorbsiyonu oldurmuyoruz
    # ama katkisini da vermiyoruz -> islem+direnc tasir.
    duvar_faktor_long = duvar_teyitli_long if duvar_teyitli_long > 0 else 0.35
    duvar_faktor_short = duvar_teyitli_short if duvar_teyitli_short > 0 else 0.35
    absorbsiyon_long = (satis_yogunlugu ** 0.50) * (fiyat_direnci_long ** 0.25) * (duvar_faktor_long ** 0.25)
    absorbsiyon_short = (alis_yogunlugu ** 0.50) * (fiyat_zayifligi_short ** 0.25) * (duvar_faktor_short ** 0.25)

    # =================== BAĞLAM ÇARPANLARI ===================
    # (Spot teyidi + borsa ayrismasi + funding — v2'den korundu)
    spot_carpani_long = 1.0
    spot_carpani_short = 1.0
    if d_vadeli < 0 and d_spot < 0:
        spot_carpani_long = 1.15
    if d_vadeli > 0 and d_spot < 0:
        spot_carpani_short = 1.15
        spot_carpani_long = 0.80
    if d_vadeli > 0 and d_spot > 0:
        spot_carpani_short = 0.85

    # (D) BORSA AYRIŞMASI — Binance, Bybit, OKX ayni yone mi bakiyor?
    #     Uc borsanin ORTALAMA deltasi ve UYUMU. Ne kadar cok borsa ayni
    #     yonde bid/ask-agir ise hareket o kadar genis tabanli (guclu).
    #     Tek borsada bid-agir + digerleri notr = spoofing/lokal (zayif).
    bnb = a['bnb_delta']; byb = a['byb_delta']
    okx = a.get('okx_delta', 0.0)
    aktif = a.get('aktif_borsa', 2)
    deltalar = [bnb, byb, okx]
    # Kac borsa bid-agir (pozitif delta), kac borsa ask-agir (negatif)
    bid_agir_sayi = sum(1 for d in deltalar if d > 0.02)
    ask_agir_sayi = sum(1 for d in deltalar if d < -0.02)
    borsa_carpani_long = 1.0
    borsa_carpani_short = 1.0
    # Genis mutabakat: 3 borsanin 2+'si ayni yonde -> teyit
    if bid_agir_sayi >= 2:
        borsa_carpani_long = 1.15 if bid_agir_sayi == 3 else 1.10
    if ask_agir_sayi >= 2:
        borsa_carpani_short = 1.15 if ask_agir_sayi == 3 else 1.10

    fund = a['funding']
    fund_carpani_long = 1.0
    fund_carpani_short = 1.0
    if fund > 0.0005:
        fund_carpani_long = 0.88
        fund_carpani_short = 1.10
    elif fund < -0.0002:
        fund_carpani_long = 1.10
        fund_carpani_short = 0.90

    # (C) CVD IRAKSAMA — spot ve vadeli uyumu skoru dogrudan etkiler.
    #     Teyit (pozitif) -> hareketi guclendir. Iraksama (negatif) ->
    #     kaldiracli/kirilgan, skoru zayiflat. Buyukluk kadar etki eder.
    iraksama = pencere.get('cvd_iraksama', 0.0)
    # iraksama [-1,1] -> carpan [0.82, 1.18] araligina esle
    iraksama_carpani = 1.0 + 0.18 * iraksama
    # LONG ve SHORT ayni yonde etkilenir (uyum ikisi icin de iyi, iraksama kotu)
    iraksama_carpani_long = iraksama_carpani
    iraksama_carpani_short = iraksama_carpani

    expiry_carpani = 1.0
    if ceyreklik_expiry_yakin_mi(datetime.datetime.utcnow(), esik_saat=48):  # FIX4: UTC (yerel degil)
        expiry_carpani = 1.08

    # =================== HAM SKORLAR ===================
    long_skor = 100.0 * absorbsiyon_long * spot_carpani_long \
                * borsa_carpani_long * fund_carpani_long \
                * iraksama_carpani_long * expiry_carpani
    short_skor = 100.0 * absorbsiyon_short * spot_carpani_short \
                 * borsa_carpani_short * fund_carpani_short \
                 * iraksama_carpani_short * expiry_carpani

    # =====================================================================
    # KATMAN 4 — OI VETOSU (kirilganlik). SINYALI KESER (zayiflatmaz).
    # =====================================================================
    # Fiyat yukari + OI asagi = SHORT SQUEEZE (taze alim degil, kirilgan).
    #   -> LONG skoru tavanini SINYAL_ESIGI ALTINA cek (65 alti) -> LONG cikmaz.
    # Fiyat asagi + OI asagi = LONG TASFIYE (kirilgan dusus).
    #   -> SHORT skoru tavanini SINYAL_ESIGI altina cek -> SHORT cikmaz.
    # ===== KATMAN 4 — OI. v7.3: ARTIK IKI SORU SORULUYOR =====
    # 1) OI artti mi, azaldi mi?  2) Bu degisim ZORLA mi, GONULLU mu?
    #
    # Ayni "OI dusuyor" gorunumu, likidasyon dikeniyle ESZAMANLIYSA bambaska
    # bir sey anlatir: o bir pozisyon KACISI degil, bir TASFIYEDIR.
    # Tasfiye, sinyalin karsiti degil, sinyalin KENDISI olabilir.
    #
    # Kanit (1 Tem 2026): fitil aninda OI 292K->288K DIKEY dustu; ayni anda
    # grafigin en buyuk likidasyon dikeni vardi. O OI dususu "short'lar
    # kapaniyor" degildi — zorla kapatilan LONG'lardi. Yakit ise SONRASINDA
    # geldi (288K->272K, 30 saat). Dikey dusus (mekanik) ile yavas erime
    # (yakit) AYRI seylerdir; kohort ikisini ayri olcer.
    tasfiye_long = a.get('tasfiye_long_yogunluk', 0.0) >= TASFIYE_DIKEN_CARPANI
    tasfiye_short = a.get('tasfiye_short_yogunluk', 0.0) >= TASFIYE_DIKEN_CARPANI
    oi_dustu = d_oi <= -TASFIYE_OI_MIN_PCT
    zorla_long_tasfiye = tasfiye_long and oi_dustu     # long'lar ZORLA kapatildi
    zorla_short_tasfiye = tasfiye_short and oi_dustu   # short'lar ZORLA kapatildi

    VETO_TAVANI = SINYAL_ESIGI - 15.0  # 75: veto rejiminde sinyal matematiksel imkansiz
    rejim = "NOTR"
    if d_fiyat > 0.02 and d_oi > 0.05:
        rejim = "TAZE_ALIM"; long_skor *= 1.20      # saglikli -> odul
    elif d_fiyat > 0.02 and d_oi < -0.05:
        # AYNI DESEN, UC ANLAM — likidasyon dikeninin YONU ayirt eder (v7.3.1).
        # Eski iki-yollu dal, kanonik supurmenin GERI-ALIM barini (long flush +
        # fiyat toparlanmasi) SHORT_SQUEEZE sayiyordu — spec §1 ile §6 celisiyordu.
        if zorla_long_tasfiye:
            # Long'lar AZ ONCE zorla flush edildi ve fiyat GERI ALIYOR.
            # OI dususu = flush'in KENDISI (mekanik), "yakitsizlik" DEGIL.
            # 1 Tem'in geri-alim bari tam olarak budur.
            rejim = "TASFIYE_SONRASI_DONUS"
            if not TASFIYE_AYRIMI_AKTIF:
                long_skor = min(long_skor, VETO_TAVANI)   # FAZ 1: veto AYNEN durur
        elif zorla_short_tasfiye:
            # Short'lar ZORLA kapatiliyor = zorunlu ALIM = yukselisin motoru.
            # Bu "yakitsiz squeeze" DEGILDIR.
            rejim = "SHORT_TASFIYE"
            if not TASFIYE_AYRIMI_AKTIF:
                long_skor = min(long_skor, VETO_TAVANI)   # FAZ 1: veto AYNEN durur
            # FAZ 2: veto kalkar. BONUS YOK — olculmemis varsayim skora gomulmez;
            # bonus AYRI bir hipotezdir, ayri olculur.
        else:
            rejim = "SHORT_SQUEEZE"
            long_skor = min(long_skor, VETO_TAVANI)       # gonullu cikis: veto DOGRU
    elif d_fiyat < -0.02 and d_oi > 0.05:
        rejim = "TAZE_SATIS"; short_skor *= 1.20
        # v7.3 NOT: bu bonus, supurme oncesi "surunme" fazinda dibin DIBINDE
        # short'u odullendiriyor olabilir. FAZ 1'de DOKUNMA; SILAHLI durumdayken
        # kac kez verildigi kohort sayacina yazilir — FAZ 2'nin ikinci sorusu bu.
    elif d_fiyat < -0.02 and d_oi < -0.05:
        if zorla_long_tasfiye:
            # Long'lar ZORLA kapatiliyor = kapitulasyon = donus adayi.
            # "Kirilgan dusus" DEGIL, "satici tukendi" olabilir.
            rejim = "LONG_KAPITULASYON"
            short_skor = min(short_skor, VETO_TAVANI)   # short'a girme (her iki fazda)
            # FAZ 2: burada LONG kapisi acilabilir. FAZ 1: sadece etiketle.
        else:
            rejim = "LONG_TASFIYE"
            short_skor = min(short_skor, VETO_TAVANI)   # VETO

    long_skor = max(0.0, min(100.0, long_skor))
    short_skor = max(0.0, min(100.0, short_skor))

    if rejim == "NOTR":
        if absorbsiyon_long > 0.45:
            rejim = "DIP_TOPLAMA"
        elif absorbsiyon_short > 0.45:
            rejim = "TEPE_DAGITIM"

    # ============ v7.4 — EMİLİM AYRIMI (olcum; FAZ 1'de skoru ETKILEMEZ) ============
    # Uc vekil metrik: emilim esnekligi (birim satis basina fiyat), emilim borsasi
    # (satis agirligi spot mu vadeli mi), satici tukenmesi (satis hizi sonuyor mu —
    # ozet dongusunde hesaplanip skor_girdi ile gelir; burada seri yok).
    esik_s = a.get('esik_spot_neg', -2000.0)
    esik_c_raw = a.get('esik_c_neg', -2000.0)   # ham (fonksiyonlar icinde abs alinir)
    emilim_esnek = _emilim_esnekligi(d_fiyat, d_vadeli, d_spot,
                                     esik_c_raw, esik_s, a.get('esik_volatilite', 0.0))
    # v7.5: artik GERCEK spot order book gecirilir (yoksa v7.4 vekiline duser).
    emilim_bors, emilim_spot_eg, emilim_perp_eg, emilim_spot_pay = _emilim_borsasi(
        d_vadeli, d_spot, esik_c_raw, esik_s,
        spot_bid_d=a.get('spot_bid_d'), spot_ask_d=a.get('spot_ask_d'),
        perp_bid_d=a.get('bid_d'), perp_ask_d=a.get('ask_d'),
        spot_ob_yasi_sn=a.get('spot_ob_yasi_sn'))
    satici_tuk = a.get('satici_tukenmesi', False)
    sonme_orani = a.get('sonme_orani', None)
    alici_tuk = a.get('alici_tukenmesi', False)         # v7.6: ALICI tukenmesi (dagitim)
    alici_sonme = a.get('alici_sonme_orani', None)
    emilim = {
        'emilim_esnekligi': round(emilim_esnek, 4) if emilim_esnek is not None else None,
        'emilim_borsasi': emilim_bors,
        'emilim_spot_pay': round(emilim_spot_pay, 4) if emilim_spot_pay is not None else None,
        # v7.5: DEFTER egilimleri. (bid-ask)/(bid+ask). None = olculemedi (0.0 DEGIL).
        'spot_egilim': emilim_spot_eg,
        'perp_egilim': emilim_perp_eg,
        'satici_tukenmesi': bool(satici_tuk),
        'sonme_orani': round(sonme_orani, 4) if sonme_orani is not None else None,
        'alici_tukenmesi': bool(alici_tuk),             # v7.6
        'alici_sonme_orani': round(alici_sonme, 4) if alici_sonme is not None else None,
        'esik_spot_neg': round(esik_s, 0),
        # v7.6: COK-BORSALI SPOT mutabakati — kac spot borsasi taze, kacinda
        # bid/ask-agir. Tek borsa spoof'a acik; 2-3 borsa ayni yon = gercek.
        'spot_borsa_sayisi': a.get('spot_borsa_sayisi', 0),
        'spot_bid_agir_sayi': a.get('spot_bid_agir_sayi', 0),
        'spot_ask_agir_sayi': a.get('spot_ask_agir_sayi', 0),
        # v7.7: PERP mutabakati (spot ile simetrik). Perp defteri zaten 3 borsa
        # (Binance/Bybit/OKX) toplaniyordu; artik KAC borsada ayni yon oldugu da
        # sayilir. SADECE olcum — skoru ETKILEMEZ (skor yolu duvar haritasindan gelir).
        'perp_borsa_sayisi': a.get('perp_borsa_sayisi', 0),
        'perp_bid_agir_sayi': a.get('perp_bid_agir_sayi', 0),
        'perp_ask_agir_sayi': a.get('perp_ask_agir_sayi', 0),
    }
    # ---- v7.6: GERCEK DEFTER egilimiyle teyit (v7.5 etiket uyumsuzlugu duzeltildi) --
    # ONCEKI HATA: zenginlestirme 'emilim_bors == VADELI' kontrol ediyordu ama
    # v7.5 _emilim_borsasi artik 'PERP'/'SPOT'/'HER_IKISI' donuyor -> DIP_TOPLAMA_PERP
    # HIC ATESLENMIYORDU. Artik teyit dogrudan DEFTER egiliminden gelir (spot bid/ask
    # agir mi), etiket adina bagli degil.
    spot_bid_agir = emilim_spot_eg is not None and emilim_spot_eg >= EMILIM_EGILIM_ESIGI
    spot_ask_agir = emilim_spot_eg is not None and emilim_spot_eg <= -EMILIM_EGILIM_ESIGI
    perp_bid_agir = emilim_perp_eg is not None and emilim_perp_eg >= EMILIM_EGILIM_ESIGI
    perp_ask_agir = emilim_perp_eg is not None and emilim_perp_eg <= -EMILIM_EGILIM_ESIGI

    # ---- Rejim zenginlestirme — TOPLAMA (satici tukenmesi + SPOT bid) ve simetrigi
    #      DAGITIM (alici tukenmesi + SPOT ask). Faz 1'de SADECE etiket; skoru ETKILEMEZ.
    if rejim == "DIP_TOPLAMA" and emilim_esnek is not None and emilim_esnek < EMILIM_YOK_ESIK:
        if emilim_esnek < EMILIM_GUCLU_ESIK and satici_tuk and spot_bid_agir:
            rejim = "DIP_TOPLAMA_SPOT"       # envanter alimi DEFTERDE teyitli
        elif not satici_tuk:
            rejim = "DIP_TOPLAMA_TEYITSIZ"   # emilim var, YON belirsiz (bugunku)
        elif perp_bid_agir and not spot_bid_agir:
            rejim = "DIP_TOPLAMA_PERP"       # kaldiracli destek, envanter YOK -> kirilgan
        else:
            rejim = "DIP_TOPLAMA_TEYITSIZ"
    elif rejim == "TEPE_DAGITIM" and emilim_esnek is not None and emilim_esnek < EMILIM_YOK_ESIK:
        # SIMETRIK: dagitan balina agresif ALICILARIN uzerine satar; alici havuzu
        # KURUYUNCA (alici tukenmesi) + spot ASK-agir (gercek coin dagitimi) -> tepe biter.
        if emilim_esnek < EMILIM_GUCLU_ESIK and alici_tuk and spot_ask_agir:
            rejim = "TEPE_DAGITIM_SPOT"      # gercek spot dagitimi, alici tukeniyor
        elif not alici_tuk:
            rejim = "TEPE_DAGITIM_TEYITSIZ"  # emilim var, YON belirsiz
        elif perp_ask_agir and not spot_ask_agir:
            rejim = "TEPE_DAGITIM_PERP"      # kaldiracli, envanter yok
        else:
            rejim = "TEPE_DAGITIM_TEYITSIZ"

    # =================== v5 SİNYAL — BALİNA DİSİPLİNİ ===================
    # Balina her harekete tepki vermez. Sinyal = TÜM koşulların KESİŞİMİ.
    # Ortalama/telafi yok: tek bir katman zayıfsa sinyal YOK.
    sinyal = "BEKLE"
    ve_kapisi_log = ""

    # -- VE-KAPISI 1: her kritik katman kendi minimumunu GEÇMELİ --
    # v9.7 (KULLANICI KARARI — Faz 2): 'duvar' kapisi KALDIRILDI. Gerekce:
    # duvar verisi 60sn'lik REST derinlik FOTOGRAFI — emir defteri milisaniyede
    # degisir, fotograf spoof'a/guruleye acik; gercek zamanli L2 imkanimiz yok.
    # Order book artik HICBIR karari etkilemez; toplama/kayit/panel YASAR
    # (duvar_teyitli skor bileseni KAYITTIR — skorlar birebir korunur, fark=0
    # skor kiyasi yasar). ob_olcum hakemleri (v9.6) dolmaya devam eder —
    # "geri acalim mi" sorusu ileride kanitla cevaplanabilir.
    long_kapilar = {
        "islem": satis_yogunlugu >= VE_ISLEM_MIN,
        "direnc": fiyat_direnci_long >= VE_DIRENC_MIN,
    }
    short_kapilar = {
        "islem": alis_yogunlugu >= VE_ISLEM_MIN,
        "direnc": fiyat_zayifligi_short >= VE_DIRENC_MIN,
    }
    long_ve = all(long_kapilar.values())
    short_ve = all(short_kapilar.values())

    # -- VE-KAPISI 2: süreç bağlamı (trende karşı sinyal YASAK) --
    # surec: a['surec_rejim'] — dagitim surerken LONG verilmez, toplama
    # surerken SHORT verilmez. Surec olgunlasip tukenme 3+ olursa ters
    # yon serbest kalir (donus artik gercekci).
    surec_rejim = a.get('surec_rejim', 'NOTR')
    surec_tukenme = a.get('surec_tukenme', 0)
    # LONG_KAPITULASYON bilerek hicbir ailede YOK: es-ailesi LONG_TASFIYE de
    # hicbir gate ailesinde degildi — eklemek davranisi DEGISTIRIRDI.
    dagitim_ailesi = surec_rejim in ('TEPE_DAGITIM', 'SHORT_SQUEEZE', 'TAZE_SATIS')
    if not TASFIYE_AYRIMI_AKTIF:
        # FAZ 1: tasfiye rejimleri es-aile -> davranis v7.2 ile BIREBIR korunur.
        # FAZ 2 (v7.3.1 Duzeltme 2): aileden CIKARLAR — yoksa OI vetosu kalkar
        # ama VE-kapisi-2 LONG'u yine keser ve Faz 2 bir NO-OP olur
        # (simule edildi: skor 100.0, sinyal BEKLE — sebebini haftalarca ararsin).
        dagitim_ailesi = dagitim_ailesi or surec_rejim in (
            'SHORT_TASFIYE', 'TASFIYE_SONRASI_DONUS')
    if not EMILIM_AYRIMI_AKTIF:
        # FAZ 1: TEPE_DAGITIM_* es-aile -> davranis v7.5 ile BIREBIR (simetrik).
        dagitim_ailesi = dagitim_ailesi or surec_rejim in (
            'TEPE_DAGITIM_SPOT', 'TEPE_DAGITIM_TEYITSIZ', 'TEPE_DAGITIM_PERP')
    toplama_ailesi = surec_rejim in ('DIP_TOPLAMA', 'TAZE_ALIM')
    if not EMILIM_AYRIMI_AKTIF:
        # FAZ 1: DIP_TOPLAMA_* es-aile -> davranis v7.3.2 ile BIREBIR.
        # FAZ 2: aileden CIKARLAR (rejim adi -> VE-kapisi-2; v7.3.1 NO-OP dersi).
        toplama_ailesi = toplama_ailesi or surec_rejim in (
            'DIP_TOPLAMA_SPOT', 'DIP_TOPLAMA_TEYITSIZ', 'DIP_TOPLAMA_PERP')
    if dagitim_ailesi and surec_tukenme < 3:
        long_ve = False   # dagitim aktifken dususe karsi LONG yok
    if toplama_ailesi and surec_tukenme < 3:
        short_ve = False  # toplama aktifken yukselise karsi SHORT yok

    # -- VE-KAPISI 3: maliyet çıtası — kurulum yeterli hareket vaat etmeli --
    # En yakin karsi duvara mesafe, MALIYET_CITASI_PCT'den buyukse hedef var.
    # (hedefe kadar bos alan = hareket alani; duvar dipte/tepede ise alan yok)
    hedef_var_long = True
    hedef_var_short = True
    fiyat = a.get('fiyat', 0)
    if fiyat > 0:
        en_yakin_ask = a.get('en_yakin_ask_fiyat', 0)   # ilk ciddi satim duvari
        en_yakin_bid = a.get('en_yakin_bid_fiyat', 0)   # ilk ciddi alim duvari
        if en_yakin_ask > fiyat:
            hedef_var_long = ((en_yakin_ask - fiyat) / fiyat * 100) >= MALIYET_CITASI_PCT
        if 0 < en_yakin_bid < fiyat:
            hedef_var_short = ((fiyat - en_yakin_bid) / fiyat * 100) >= MALIYET_CITASI_PCT

    # -- NİHAİ KARAR: skor + marj + VE-kapıları --
    # v9.7: 'hedef' (maliyet citasi) sarti KALDIRILDI — en_yakin_ask/bid duvar
    # mesafesi ayni 60sn REST fotografindan geliyordu (kullanici karari: order
    # book karara girmez). hedef_var_* hesabi KAYIT olarak yukarida durur.
    if (long_skor >= SINYAL_ESIGI and (long_skor - short_skor) >= SINYAL_MARJI
            and long_ve):
        sinyal = "LONG"
    elif (short_skor >= SINYAL_ESIGI and (short_skor - long_skor) >= SINYAL_MARJI
            and short_ve):
        sinyal = "SHORT"

    # Log için hangi kapının kapalı olduğunu kaydet (öğrenmek için)
    # v9.7: 'duvar' ve 'hedef' artik kapi degil — kapali listesine giremezler
    if sinyal == "BEKLE" and max(long_skor, short_skor) >= SINYAL_ESIGI:
        taraf = long_kapilar if long_skor > short_skor else short_kapilar
        kapali = [k for k, v in taraf.items() if not v]
        if (long_skor > short_skor and dagitim_ailesi and surec_tukenme < 3):
            kapali.append("surec")
        if (short_skor > long_skor and toplama_ailesi and surec_tukenme < 3):
            kapali.append("surec")
        ve_kapisi_log = f" VE-RED:{','.join(kapali) if kapali else 'marj'}"

    # ---- v9.3 GOLGE (spec adi "v9.1 golge sinyal gorunurluk"; v9.1 etiketi repoda
    # panel seridinde kullanildigi icin kod etiketi v9.3): "skor esigi gecti ama
    # kapi reddetti" bilgisi SALT KAYIT olarak turetilir. MUTLAK KURAL: yukaridaki
    # nihai karar (sinyal) DEGISMEZ; golge asla sinyale donusmez, hicbir kosul
    # golge_* okumaz. KRITIK UYARI (spec F): golge = kapinin REDDETTIGI kurulum —
    # istatistiksel olarak zayiftir; VERIDIR, islem cagrisi DEGILDIR. ----
    # v9.3-GOLGE BASLA (kabul testleri bu blogu marker'la calistirir)
    golge_yon = None
    golge_kapi = None
    golge_skor = None
    if sinyal == "BEKLE" and max(long_skor, short_skor) >= SINYAL_ESIGI:
        # marj da gecmisse gercek golge var; marj gecmediyse flip-flop bolgesi —
        # golge bile degil, None kalir (sifir tuzagi: uydurma yok)
        if long_skor >= short_skor and (long_skor - short_skor) >= SINYAL_MARJI:
            golge_yon = "LONG"
        elif short_skor > long_skor and (short_skor - long_skor) >= SINYAL_MARJI:
            golge_yon = "SHORT"
        if golge_yon:
            # kapali listesi yukarida ZATEN hesaplandi (ayni kosul altinda) —
            # yeniden hesap yok, mevcut degisken okunur (spec A)
            golge_kapi = ",".join(kapali) if kapali else None
            golge_skor = max(long_skor, short_skor)
    # v9.3-GOLGE BITIR

    duvar_durum = "teyitli" if (duvar_teyitli_long > 0 or duvar_teyitli_short > 0) else "teyitsiz"
    # v7.4: emilim metrikleri aciklamaya (log'da her dakika gorunur, §3/§5)
    emilim_log = ""
    if emilim_esnek is not None:
        _sp = f"{emilim_spot_pay:.2f}" if emilim_spot_pay is not None else "—"
        _seg = f"{emilim_spot_eg:+.2f}" if emilim_spot_eg is not None else "—"
        emilim_log = (f" | EMILIM esnek={emilim_esnek:.2f} bors={emilim_bors}"
                      f"(pay={_sp} spotEg={_seg}) satTuk={'E' if satici_tuk else 'H'}"
                      f" aliTuk={'E' if alici_tuk else 'H'}")
    aciklama = (f"absL={absorbsiyon_long:.2f} absS={absorbsiyon_short:.2f} "
                f"dFiyat={d_fiyat:+.3f}% dOI={d_oi:+.2f}% "
                f"dVadeliCVD={d_vadeli:+,.0f} dSpotCVD={d_spot:+,.0f} "
                f"iraksama={iraksama:+.2f} borsa={aktif_borsa} "
                f"duvar={duvar_durum} rejim={rejim}{ve_kapisi_log}{emilim_log}")

    # v9.3: donus 6 -> 9 eleman (tek cagri yeri var, dogrulandi). Ilk 6 BIREBIR
    # ayni sirada — 500-esdegerlik testi e[0..3]/y[0..3] kiyasi etkilenmez.
    return (long_skor, short_skor, sinyal, rejim, aciklama, emilim,
            golge_yon, golge_kapi, golge_skor)


# =========================================================================
# v7.3 — TASFIYE KOHORTU YARDIMCILARI
# =========================================================================
# Kohort balina_ayarlar['tasfiye_kohortu'] JSONB'sinde yasar (sema degisikligi
# YOK). Iki thread yazar (ozet: yeni olay; geri_test: getiri backfill) —
# read-modify-write kayiplarina karsi ayri kilit. durum.lock KULLANILMAZ:
# ag cagrisi o kilidin altinda WS handler'larini bloklardi.
_kohort_lock = threading.Lock()

def _tasfiye_kohortuna_ekle(yeni_olaylar):
    """Basari bool'u dondurur; cagiran (tampon) yalnizca True'da temizler."""
    try:
        with _kohort_lock:
            # v8.7 RMW korumasi: okuma HATASI 'bos kohort' sanilirsa sonraki
            # upsert TUM olay gecmisini ezer (denetim bulgusu). Hatada bu tur atlanir.
            _ok_k, kayit = _ayarlar_oku_katilim("tasfiye_kohortu")
            if not _ok_k:
                logging.warning("tasfiye_kohortu okunamadi — yazim atlandi (gecmis korunur)")
                return False
            veri = (kayit or {}).get('deger') or {}
            if isinstance(veri, str):
                try:
                    veri = json.loads(veri)
                except Exception:
                    veri = {}
            olaylar = veri.get('olaylar', [])
            olaylar.extend(yeni_olaylar)
            if len(olaylar) > KOHORT_AZAMI_KAYIT:
                olaylar = olaylar[-KOHORT_AZAMI_KAYIT:]
            veri['olaylar'] = olaylar
            meta = veri.get('meta', {})
            # max(): restart bellek sayacini sifirlar; kalici deger GERIYE gitmesin
            meta['taze_satis_silahli_sayac'] = max(
                int(meta.get('taze_satis_silahli_sayac') or 0),
                getattr(ozet_ve_analiz_dongusu, '_taze_satis_silahli', 0))
            meta['guncelleme'] = datetime.datetime.utcnow().isoformat()
            veri['meta'] = meta
            return _ayarlar_yaz("tasfiye_kohortu", veri)
    except Exception as e:
        logging.warning(f"Tasfiye kohortu yazma hatasi: {e}")
        return False


def _aralik_min_max(zamanli, t0, ufuk_dk):
    """t0 ile t0+ufuk arasindaki TUM barlarin (min, max, kapanis) fiyati.
    Ufuk SONU fiyatina bakmak yetmez: islem 60. dakikada +%2'de olabilir ama
    8. dakikada stop'unu vurmus olabilir — o +%2 HAYALI kardir (spec §8.2)."""
    hedef = t0 + datetime.timedelta(minutes=ufuk_dk)
    fiyatlar = []
    kapanis = None
    for (t2, s2) in zamanli:
        if t0 < t2 <= hedef:
            f = float(s2.get('anlik_fiyat') or 0)
            if f > 0:
                fiyatlar.append(f)
        if t2 >= hedef and kapanis is None:
            f = float(s2.get('anlik_fiyat') or 0)
            kapanis = f if f > 0 else None
    if not fiyatlar:
        return None, None, kapanis
    return min(fiyatlar), max(fiyatlar), kapanis


def _kohort_ileri_olc(zamanli, simdi, ufuklar, maliyet_pct):
    """
    v7.3 — kohortun ileri getirisi + MAE/MFE + stop_vuruldu + gecikmeli teyit.
    Kumeleme (spec §8.3): ayni yonde <=KOHORT_KUME_DK arayla VEYA ayni seviyeden
    (±1 x vol) turemis girdiler ayni kume; ozet YALNIZ kume baslarindan.
    """
    try:
        with _kohort_lock:
            # v8.7 RMW korumasi: okuma HATASI 'bos kohort' sanilirsa sonraki
            # upsert TUM olay gecmisini ezer (denetim bulgusu). Hatada bu tur atlanir.
            _ok_k, kayit = _ayarlar_oku_katilim("tasfiye_kohortu")
            if not _ok_k:
                logging.warning("tasfiye_kohortu okunamadi — yazim atlandi (gecmis korunur)")
                return False
            veri = (kayit or {}).get('deger') or {}
            if isinstance(veri, str):
                try:
                    veri = json.loads(veri)
                except Exception:
                    return
            olaylar = veri.get('olaylar', [])
            if not olaylar:
                return
            degisti = False

            for o in olaylar:
                try:
                    t0 = datetime.datetime.fromisoformat(
                        str(o.get('zaman', '')).replace('Z', '+00:00')).replace(tzinfo=None)
                except Exception:
                    continue
                f0 = float(o.get('giris_fiyati') or 0)
                if f0 <= 0:
                    continue
                yon = 1 if o.get('yon') == 'LONG' else -1
                stop_ref = float(o.get('stop_ref') or 0)

                for ufuk in ufuklar:
                    anahtar = f"{ufuk}dk"
                    if anahtar in (o.get('getiri') or {}):
                        continue
                    if anahtar in (o.get('olculemez') or []):
                        continue
                    if (simdi - t0).total_seconds() / 60.0 < ufuk:
                        continue
                    # Kesinti korumasi: t0, eldeki pencerenin basindan ONCEYSE
                    # min/max KISMI olur -> MAE kucuk, stop_vuruldu yanlis-negatif
                    # (tehlikeli yon: kalibi oldugundan GUVENLI gosterir). Olcme,
                    # kalici olarak 'olculemez' isaretle (sonsuz yeniden tarama da biter).
                    if zamanli and t0 < zamanli[0][0]:
                        o.setdefault('olculemez', []).append(anahtar)
                        degisti = True
                        continue
                    mn, mx, kapanis = _aralik_min_max(zamanli, t0, ufuk)
                    if mn is None or kapanis is None:
                        continue
                    getiri = (kapanis / f0 - 1) * 100 * yon
                    if yon > 0:
                        mfe = (mx / f0 - 1) * 100
                        mae = (mn / f0 - 1) * 100
                        stop_vuruldu = stop_ref > 0 and mn < stop_ref * (1 - 0.05 / 100)
                    else:
                        mfe = (f0 - mn) / f0 * 100
                        mae = (f0 - mx) / f0 * 100
                        stop_vuruldu = stop_ref > 0 and mx > stop_ref * (1 + 0.05 / 100)
                    o.setdefault('getiri', {})[anahtar] = round(getiri, 4)
                    o.setdefault('mae_mfe', {})[anahtar] = {
                        "mfe": round(mfe, 4), "mae": round(mae, 4),
                        "stop_vuruldu": bool(stop_vuruldu)}
                    degisti = True

                # GECIKMELI TEYIT (kapi degil, olcum): spot CVD / OI ne zaman dondu?
                # Not: bu sorgunun penceresi max(ufuk)+30dk — 720dk'lik tam izleme
                # yerine pencere iciyle sinirli; null = "pencerede donmedi" bilgisidir.
                ham = o.get('ham') or {}
                if o.get('spot_teyit_gecikmesi_dk') is None:
                    # DIKKAT — COZUNURLUK TUZAGI (spec risk #5): spot_cvd kolonu
                    # BIRIKIMLI degil, 15-dk KAYAN AKIS toplamidir. Giris anindaki
                    # asiri-negatif zirveyle kiyaslamak, zirve pencereden cikinca
                    # ~15dk'da mekanik "dondu" derdi (1 Tem gercegi ~600dk!).
                    # Durust olcu: akisin kendisi islem yonunde SIFIRI GECTIGI ilk
                    # bar — "net spot alimi belirdi" (LONG icin sc>0, SHORT sc<0).
                    for (t2, s2) in zamanli:
                        if t2 <= t0:
                            continue
                        sc = s2.get('spot_cvd')
                        if sc is None:
                            continue
                        dondu = (float(sc) > 0) if yon > 0 else (float(sc) < 0)
                        if dondu:
                            o['spot_teyit_gecikmesi_dk'] = round(
                                (t2 - t0).total_seconds() / 60.0, 1)
                            degisti = True
                            break
                if o.get('oi_erime_gecikmesi_dk') is None and ham.get('oi_giris'):
                    # OI erimesi = TUZAKTAKI pozisyonlarin kapanmasi; OI, islem
                    # yonunden BAGIMSIZ olarak DUSER (tepe supurmesinde tuzakli
                    # long'lar kapanir -> OI yine dusmeli). Eski kod SHORT icin
                    # yukselis bekliyordu — tam tersi (dogrulayici tespiti).
                    for (t2, s2) in zamanli:
                        if t2 <= t0:
                            continue
                        oi2 = s2.get('open_interest')
                        if oi2 is None or float(oi2) <= 0:
                            continue
                        if float(oi2) < ham['oi_giris']:
                            o['oi_erime_gecikmesi_dk'] = round(
                                (t2 - t0).total_seconds() / 60.0, 1)
                            degisti = True
                            break

            # ---- KUMELEME + 2x2 OZET (yalniz kume baslarindan) ----
            sirali = sorted(olaylar, key=lambda x: str(x.get('zaman', '')))
            for i, o in enumerate(sirali):
                kume_ici = False
                try:
                    ti = datetime.datetime.fromisoformat(
                        str(o['zaman']).replace('Z', '+00:00')).replace(tzinfo=None)
                except Exception:
                    o['kume_ici'] = False
                    continue
                vol_i = (o.get('ham') or {}).get('esik_volatilite') or 0.0
                for p in sirali[:i]:
                    try:
                        tp = datetime.datetime.fromisoformat(
                            str(p['zaman']).replace('Z', '+00:00')).replace(tzinfo=None)
                    except Exception:
                        continue
                    if p.get('yon') == o.get('yon') and \
                            0 <= (ti - tp).total_seconds() / 60.0 <= KOHORT_KUME_DK:
                        kume_ici = True
                        break
                    # ayni seviyeden tureyen girdiler HER ZAMAN ayni kume
                    # (restart'ta cooldown kacabilir — spec §8.3)
                    if o.get('seviye') and p.get('seviye') and vol_i > 0 and \
                            abs(o['seviye'] - p['seviye']) / o['seviye'] * 100 <= vol_i:
                        kume_ici = True
                        break
                if o.get('kume_ici') != kume_ici:
                    o['kume_ici'] = kume_ici
                    degisti = True

            ozet = {}
            for o in sirali:
                if o.get('kume_ici'):
                    continue
                hucre = (f"tasfiye_{'var' if o.get('tasfiye_var') else 'yok'}"
                         f"__supurme_{'var' if o.get('supurme_var') else 'yok'}")
                h = ozet.setdefault(hucre, {"n": 0})
                h["n"] += 1
                for anahtar, g in (o.get('getiri') or {}).items():
                    u = h.setdefault(anahtar, {"n": 0, "dogru": 0, "toplam": 0.0, "stop": 0})
                    u["n"] += 1
                    u["toplam"] += g
                    if g > 0:
                        u["dogru"] += 1
                    mm = (o.get('mae_mfe') or {}).get(anahtar) or {}
                    if mm.get('stop_vuruldu'):
                        u["stop"] += 1
            for hucre, h in ozet.items():
                for anahtar in list(h.keys()):
                    if anahtar == "n":
                        continue
                    u = h[anahtar]
                    if u["n"]:
                        ort = u["toplam"] / u["n"]
                        h[anahtar] = {"n": u["n"],
                                      "isabet": round(100.0 * u["dogru"] / u["n"], 1),
                                      "ort_getiri": round(ort, 4),
                                      "net_getiri": round(ort - maliyet_pct, 4),
                                      "stop_orani": round(100.0 * u["stop"] / u["n"], 1)}
            # Yazim amplifikasyonu korumasi: 'ozet dolu' surekli True olur ve her
            # 180sn'de ~yuzlerce KB'lik ayni JSONB yeniden yazilirdi (hatalar
            # muzesi #1: API istismari). Yalnizca GERCEK degisimde yaz.
            if degisti or ozet != (veri.get('ozet_2x2') or {}):
                veri['olaylar'] = olaylar
                veri['ozet_2x2'] = ozet
                veri['guncelleme'] = simdi.isoformat()
                _ayarlar_yaz("tasfiye_kohortu", veri)
            return ozet
    except Exception as e:
        logging.warning(f"Kohort ileri olcum hatasi: {e}")
        return None


# =========================================================================
# ÖZET & ANALİZ MOTORU (v2 beyni ile)
# =========================================================================
def ozet_ve_analiz_dongusu():
    logging.info("Ozet/analiz motoru v2 baslatildi (baglamsal skorlama).")
    time.sleep(15)

    while True:
        try:
            with durum.lock:
                anlik_fiyat = durum.anlik_fiyat
                funding_rate = durum.funding_rate
                open_interest = durum.open_interest

                ws_vadeli_cvd = sum(q for _, q in durum.trade_gecmisi)
                ws_spot_cvd = sum(q for _, q in durum.spot_trade_gecmisi)
                # FIX3: coinalyze_cvd_saglikli latch'i bir kez True olunca hic
                # resetlenmiyordu -> Coinalyze olurse DONMUS CVD ile sinyal cikabilirdi.
                # Artik son BASARILI hesabin uzerinden CVD_BAYATLIK_SN gectiyse kaynak
                # GUVENSIZ sayilir; kapi reddeder ve WS-yedege duser (o da reddedilir).
                cvd_taze = (time.time() - durum.coinalyze_cvd_zaman) < CVD_BAYATLIK_SN
                cvd_kaynak_saglikli = durum.coinalyze_cvd_saglikli and cvd_taze  # A: kalite girdisi
                if cvd_kaynak_saglikli:
                    calculated_cvd = durum.agg_vadeli_cvd
                    spot_cvd = durum.agg_spot_cvd
                else:
                    calculated_cvd = ws_vadeli_cvd
                    spot_cvd = ws_spot_cvd
                # v7.8: kaynak ETIKETI seriye yazilir. AGG (Coinalyze) ve WS
                # (Binance-yedek) TABANLARI farkli — gecisin ustunden delta almak
                # gurultu uretir; okuyucular ayni-kaynak sartini bununla denetler.
                cvd_kaynak_etiketi = 'AGG' if cvd_kaynak_saglikli else 'WS'

                # ---- %1 DERİNLİK: Binance, Bybit ve OKX AYRI hesaplaniyor ----
                bnb_bid_d = 0.0; bnb_ask_d = 0.0
                byb_bid_d = 0.0; byb_ask_d = 0.0
                okx_bid_d = 0.0; okx_ask_d = 0.0
                # v7.5: SPOT derinligi AYRI tutulur — vadeli duvar haritasina
                # KARISTIRILMAZ. Sebep: spot defteri bir "duvar" degil, bir ENVANTER
                # NIYETI gostergesidir. Vadeli bid = kaldiracli bahis; spot bid =
                # gercek coin alimi. Ikisini toplamak, tam da ayirt etmek istedigimiz
                # farki YOK EDERDI. Duvar vetosu/mutabakati SADECE vadeliden gelir.
                spot_bid_d = 0.0; spot_ask_d = 0.0
                spot_ob_yasi = None
                spot_borsa_sayisi = 0        # v7.6: kac spot borsasi taze
                spot_bid_agir_sayi = 0       # v7.6: kacinda spot bid-agir (mutabakat)
                spot_ask_agir_sayi = 0
                # v7.7: PERP defter mutabakati (spot ile simetrik; SADECE olcum).
                perp_borsa_sayisi = 0        # kac perp borsasi TAZE + derinlikli
                perp_bid_agir_sayi = 0       # kacinda perp bid-agir
                perp_ask_agir_sayi = 0
                perp_ob_yasi = None          # dahil edilen perp borsalari icinde en yasli
                buyuk_bidler = []; buyuk_asklar = []
                tum_bidler = []; tum_asklar = []
                # v5.2: UC-BORSALI DUVAR HARITASI
                # Her borsanin emirleri KOVA_USD'lik fiyat kovalarina toplanir.
                # Ayni kovada birden fazla borsada duvar varsa -> MUTABAKAT.
                # Tek borsada dev duvar = spoof suphesi; uc borsada ayni seviyede
                # duvar = gercek kurumsal seviye (uc borsada birden spoof zordur).
                KOVA_USD = 25.0
                bid_kovalar = {}   # kova_fiyat -> {'usdt':..., 'borsalar':set()}
                ask_kovalar = {}

                def _kovaya_ekle(kovalar, fiyat, usdt, borsa):
                    k = round(fiyat / KOVA_USD) * KOVA_USD
                    if k not in kovalar:
                        kovalar[k] = {'usdt': 0.0, 'borsalar': set()}
                    kovalar[k]['usdt'] += usdt
                    kovalar[k]['borsalar'].add(borsa)

                if anlik_fiyat > 0:
                    # v7.8: 0.99/1.01 elle yazimi EMILIM_DERINLIK_PCT'ye baglandi
                    # (sabit tanimliydi ama KULLANILMIYORDU — band tek yerden yonetilir;
                    # 1-0.01/1+0.01 = ayni sayilar, davranis BIREBIR).
                    alt_limit = anlik_fiyat * (1.0 - EMILIM_DERINLIK_PCT)
                    ust_limit = anlik_fiyat * (1.0 + EMILIM_DERINLIK_PCT)
                    # v7.6: COK-BORSALI SPOT defter (ayri hesap, duvar haritasina
                    # girmez). Her TAZE borsanin bid/ask derinligi toplanir + mutabakat
                    # sayilir (kac borsada spot bid-agir). BUG DUZELTME: v7.5 burada
                    # simdi_epoch kullaniyordu ama o 100+ satir SONRA tanimliydi ->
                    # spot ilk cekildikten sonra HER dongu NameError'la coker, motor
                    # susardi. Artik time.time() dogrudan.
                    _snow = time.time()
                    for _sb, _sa, _sz in (
                            (durum.spot_bids, durum.spot_asks, durum.spot_ob_zaman),
                            (durum.bybit_spot_bids, durum.bybit_spot_asks, durum.bybit_spot_zaman),
                            (durum.okx_spot_bids, durum.okx_spot_asks, durum.okx_spot_zaman)):
                        if _sz <= 0 or (_snow - _sz) > SPOT_OB_MAX_YAS_SN:
                            continue   # o borsa bayat/yok -> hesaba KATMA (sifir tuzagi yok)
                        b = sum(f * m for f, m in _sb.items() if f >= alt_limit)
                        a_ = sum(f * m for f, m in _sa.items() if f <= ust_limit)
                        if b <= 0 and a_ <= 0:
                            continue
                        spot_bid_d += b; spot_ask_d += a_
                        spot_borsa_sayisi += 1
                        _yas = _snow - _sz
                        spot_ob_yasi = _yas if spot_ob_yasi is None else max(spot_ob_yasi, _yas)
                        _top = b + a_
                        _eg = (b - a_) / _top if _top > 0 else 0.0
                        if _eg >= EMILIM_EGILIM_ESIGI:
                            spot_bid_agir_sayi += 1
                        elif _eg <= -EMILIM_EGILIM_ESIGI:
                            spot_ask_agir_sayi += 1
                    for fiyat, miktar in durum.bids.items():
                        if fiyat >= alt_limit:
                            usdt = fiyat * miktar
                            bnb_bid_d += usdt
                            tum_bidler.append((fiyat, usdt))
                            _kovaya_ekle(bid_kovalar, fiyat, usdt, 'BNB')
                            if usdt >= BUYUK_EMIR_ESIGI_USDT:
                                buyuk_bidler.append((fiyat, usdt))
                    for fiyat, miktar in durum.asks.items():
                        if fiyat <= ust_limit:
                            usdt = fiyat * miktar
                            bnb_ask_d += usdt
                            tum_asklar.append((fiyat, usdt))
                            _kovaya_ekle(ask_kovalar, fiyat, usdt, 'BNB')
                            if usdt >= BUYUK_EMIR_ESIGI_USDT:
                                buyuk_asklar.append((fiyat, usdt))
                    for fiyat, miktar in durum.bybit_bids.items():
                        if fiyat >= alt_limit:
                            u = fiyat * miktar
                            byb_bid_d += u
                            _kovaya_ekle(bid_kovalar, fiyat, u, 'BYB')
                    for fiyat, miktar in durum.bybit_asks.items():
                        if fiyat <= ust_limit:
                            u = fiyat * miktar
                            byb_ask_d += u
                            _kovaya_ekle(ask_kovalar, fiyat, u, 'BYB')
                    for fiyat, miktar in durum.okx_bids.items():
                        if fiyat >= alt_limit:
                            u = fiyat * miktar
                            okx_bid_d += u
                            _kovaya_ekle(bid_kovalar, fiyat, u, 'OKX')
                    for fiyat, miktar in durum.okx_asks.items():
                        if fiyat <= ust_limit:
                            u = fiyat * miktar
                            okx_ask_d += u
                            _kovaya_ekle(ask_kovalar, fiyat, u, 'OKX')

                # Birlesik derinlik (3 borsa; panel geriye donuk uyumlulugu icin)
                order_book_depth_bid_1pct = bnb_bid_d + byb_bid_d + okx_bid_d
                order_book_depth_ask_1pct = bnb_ask_d + byb_ask_d + okx_ask_d
                # Borsa deltalari (bid - ask), normalize edilmis
                bnb_toplam = bnb_bid_d + bnb_ask_d
                byb_toplam = byb_bid_d + byb_ask_d
                okx_toplam = okx_bid_d + okx_ask_d
                bnb_delta = (bnb_bid_d - bnb_ask_d) / bnb_toplam if bnb_toplam > 0 else 0.0
                byb_delta = (byb_bid_d - byb_ask_d) / byb_toplam if byb_toplam > 0 else 0.0
                okx_delta = (okx_bid_d - okx_ask_d) / okx_toplam if okx_toplam > 0 else 0.0
                # Kac borsada aktif derinlik var (spoofing dayanikliligi icin)
                aktif_borsa_sayisi = sum(1 for t in [bnb_toplam, byb_toplam, okx_toplam] if t > 0)

                # ---- v7.7: PERP defter MUTABAKATI (spot ile SIMETRIK; SADECE OLCUM) ----
                # Spot'ta oldugu gibi perp defterinin de KAC borsada ayni yonde
                # durdugunu sayariz. Tek borsa spoof'a acik; 2-3 perp borsa ayni
                # yonde = gercek kaldiracli konumlanma. BAYAT borsa SAYILMAZ (REST
                # cekimi durmussa eski defter mutabakati SISIRMESIN). ONEMLI: skor
                # yolu (order_book_depth_*/bid_d/ask_d) YUKARIDA DEGISMEDEN kalir ->
                # v7.2 esdegerligi korunur; bu blok yalnizca kohorta yazilir.
                for _pt, _pd, _pz in ((bnb_toplam, bnb_delta, durum.perp_bids_zaman),
                                      (byb_toplam, byb_delta, durum.bybit_perp_zaman),
                                      (okx_toplam, okx_delta, durum.okx_perp_zaman)):
                    if _pt <= 0 or _pz <= 0 or (_snow - _pz) > PERP_OB_MAX_YAS_SN:
                        continue
                    perp_borsa_sayisi += 1
                    _pyas = _snow - _pz
                    perp_ob_yasi = _pyas if perp_ob_yasi is None else max(perp_ob_yasi, _pyas)
                    if _pd >= EMILIM_EGILIM_ESIGI:
                        perp_bid_agir_sayi += 1
                    elif _pd <= -EMILIM_EGILIM_ESIGI:
                        perp_ask_agir_sayi += 1

                # ---- EMİR YAŞI (spoofing filtresi girdisi) ----
                simdi_ms = int(time.time() * 1000)
                mevcut_bid_fiyatlar = set()
                for fiyat, usdt in buyuk_bidler:
                    yuvarlanmis = round(fiyat)
                    mevcut_bid_fiyatlar.add(yuvarlanmis)
                    if yuvarlanmis not in durum.buyuk_bid_ilk_gorulme:
                        durum.buyuk_bid_ilk_gorulme[yuvarlanmis] = simdi_ms
                for eski in list(durum.buyuk_bid_ilk_gorulme.keys()):
                    if eski not in mevcut_bid_fiyatlar:
                        del durum.buyuk_bid_ilk_gorulme[eski]
                en_olgun_yas_sn = 0
                for ts in durum.buyuk_bid_ilk_gorulme.values():
                    en_olgun_yas_sn = max(en_olgun_yas_sn, (simdi_ms - ts) / 1000)

                mevcut_ask_fiyatlar = set()
                for fiyat, usdt in buyuk_asklar:
                    yuvarlanmis = round(fiyat)
                    mevcut_ask_fiyatlar.add(yuvarlanmis)
                    if yuvarlanmis not in durum.buyuk_ask_ilk_gorulme:
                        durum.buyuk_ask_ilk_gorulme[yuvarlanmis] = simdi_ms
                for eski in list(durum.buyuk_ask_ilk_gorulme.keys()):
                    if eski not in mevcut_ask_fiyatlar:
                        del durum.buyuk_ask_ilk_gorulme[eski]
                en_olgun_ask_yas_sn = 0
                for ts in durum.buyuk_ask_ilk_gorulme.values():
                    en_olgun_ask_yas_sn = max(en_olgun_ask_yas_sn, (simdi_ms - ts) / 1000)

                agg_liq_long = durum.agg_liq_long
                agg_liq_short = durum.agg_liq_short
                liquidation_pool_volume = agg_liq_long + agg_liq_short
                agg_oi = durum.agg_open_interest
                agg_funding = durum.agg_funding
                agg_ls_ratio = durum.agg_ls_ratio
                # v8.6: Coinalyze L/S bos/0 ise Binance global hesap-orani fallback'i
                # (yalniz TAZE ise, <10dk). Ikisi de yoksa 0 kalir -> panel durustce
                # 'veri yok' der (sifir tuzagi: uydurma deger yazilmaz).
                if (not agg_ls_ratio or agg_ls_ratio <= 0) and durum.binance_ls_ratio > 0 \
                        and (time.time() - durum.binance_ls_zaman) < 600:
                    agg_ls_ratio = durum.binance_ls_ratio
                coinalyze_ok = durum.coinalyze_saglikli

                esik_d = durum.esik_derinlik
                esik_l = durum.esik_likidasyon
                esik_c_neg = durum.esik_cvd_negatif
                esik_c_poz = durum.esik_cvd_pozitif

                # v7.3: adaptif birimler + seviyeler + WS yon-bazli likidasyon (ham metrik)
                esik_vol = durum.esik_volatilite
                esik_lik_long_med = durum.esik_lik_long_medyan
                esik_lik_short_med = durum.esik_lik_short_medyan
                esik_spot_neg = durum.esik_spot_negatif   # v7.4
                likidite_dipler = list(durum.likidite_dipler)
                likidite_tepeler = list(durum.likidite_tepeler)
                # Deque yalniz YAZIMDA budaniyor; sakin piyasada saatlerce yeni
                # forceOrder gelmez -> okuyucu kendi 5dk filtresini uygulamali,
                # yoksa ham metrik onlarca dakika bayat kalir.
                _lik_sinir_ms = (time.time() - 300) * 1000
                ws_lik_long = sum(u for (t, u, y) in durum.likidasyonlar
                                  if y == 'LONG' and t >= _lik_sinir_ms)
                ws_lik_short = sum(u for (t, u, y) in durum.likidasyonlar
                                   if y == 'SHORT' and t >= _lik_sinir_ms)

                son_guncelleme_gecen = time.time() - durum.son_guncelleme

            if agg_oi > 0:
                open_interest = agg_oi
            elif open_interest and open_interest > 0 and anlik_fiyat > 0:
                # v8.7 BIRIM (denetim bulgusu): Binance /fapi openInterest BTC ADEDI
                # doner; Coinalyze agg ise USD. Coinalyze dusunce BTC adedi (~80K)
                # USD serisiyle (~12e9) KARISIYORDU -> d_oi_pct patlar, sahte
                # TAZE_ALIM/LONG_TASFIYE rejimleri. USD'ye cevirerek seri tek birim kalir.
                open_interest = open_interest * anlik_fiyat
            if agg_funding != 0:
                funding_rate = agg_funding

            # ================= v2: ROLLING SERİYE EKLE + PENCERE DEĞİŞİMİ =================
            simdi_epoch = time.time()
            anlik_kayit = {
                'ts': simdi_epoch, 'fiyat': anlik_fiyat,
                'bid_d': order_book_depth_bid_1pct, 'ask_d': order_book_depth_ask_1pct,
                'bnb_delta': bnb_delta, 'byb_delta': byb_delta, 'okx_delta': okx_delta,
                'vadeli_cvd': calculated_cvd, 'spot_cvd': spot_cvd, 'oi': open_interest,
                'cvd_kaynak': cvd_kaynak_etiketi   # v7.8: delta ancak AYNI kaynakla alinir
            }
            with durum.lock:
                durum.gecmis_seri.append(anlik_kayit)
                seri_kopya = list(durum.gecmis_seri)

            # PENCERE_DK dakika onceki en yakin kaydi bul
            pencere = None
            if anlik_fiyat > 0 and len(seri_kopya) >= 2:
                hedef_ts = simdi_epoch - PENCERE_DK * 60
                eski = None
                for kayit in seri_kopya:
                    if kayit['ts'] <= hedef_ts:
                        eski = kayit
                    else:
                        break
                if eski is None:
                    eski = seri_kopya[0]  # yeterli gecmis yoksa en eskiyi kullan
                # En az ~3 dk gecmis olsun ki degisim anlamli olsun. UST SINIR
                # (FIX1-tutarlilik): kalibrasyon (_cvd_delta_serisi) veri boslugu
                # uzerinden eslesmeyi REDDEDER; canli pencere de ayni ust sinirla
                # (PENCERE_DK*60*2.5=750sn) sinirlanmali. Aksi halde outage/restart
                # sonrasi delik-asiri DEV delta, temiz delta'lara gore kalibre esigi
                # saturasyona itip (yogunluk=1.0) YANLIS sinyal uretebilir -> BEKLE.
                pencere_yasi = simdi_epoch - eski['ts']
                # v7.8 (FIX1 sinifi): pencerenin IKI UCU da ayni CVD kaynagindan olmali.
                # Coinalyze dusup WS-yedege gecince kalite kapisi zaten reddeder; ama
                # Coinalyze DUZELINCE kapi hemen 'guvenilir' der, oysa eski uc ~12dk
                # boyunca hala WS-tabanli olabilir -> karisik-tabanli DEV sahte delta
                # skora sizardi. Karisiksa o dakika pencere=None (VERI_BEKLENIYOR) —
                # olcemedigimizde uydurmayiz, BEKLEriz.
                if (150 <= pencere_yasi <= PENCERE_DK * 60 * 2.5 and eski['fiyat'] > 0
                        and eski.get('cvd_kaynak') == cvd_kaynak_etiketi):
                    d_vadeli = calculated_cvd - eski['vadeli_cvd']
                    d_spot = spot_cvd - eski['spot_cvd']
                    pencere = {
                        'd_fiyat_pct': (anlik_fiyat / eski['fiyat'] - 1.0) * 100.0,
                        'd_vadeli_cvd': d_vadeli,
                        'd_spot_cvd': d_spot,
                        'd_oi_pct': (open_interest / eski['oi'] - 1.0) * 100.0 if eski['oi'] > 0 else 0.0,
                        # C: CVD IRAKSAMA — spot ve vadeli AYNI yone mi bakiyor?
                        # Ayni yon = teyit (guclu). Zit yon = iraksama (kaldiracli/kirilgan).
                        # NOT: Gercek veri (balina_avcisi_data, 578 kayit) gosterdi ki
                        # spot ve vadeli CVD ZATEN benzer olcekte (medyan |spot|/|vadeli|
                        # ~5x, ikisi de USD-olcekli). d_vadeli'yi fiyatla carpmak dengeyi
                        # 0.24'ten ~15000'e ITELEYIP ozelligi BOZUYORDU -> ham karsilastirma
                        # korunuyor (denge ~0.24, calisan mutevazi ±%4 etki).
                        'cvd_iraksama': _cvd_iraksama_hesapla(d_vadeli, d_spot),
                    }

            # ================= v2: BAĞLAMSAL SKORU HESAPLA =================
            # v5.2: en yakin CIDDI duvar — artik UC BORSA birlesik kovalardan.
            # Sadece MUTABAKATLI (2+ borsa) VEYA cok buyuk tek-borsa duvarlari sayilir;
            # boylece tek borsadaki spoof duvar hedef sanilmaz.
            # v7.1: bariyer esigi HEDEF_DUVAR_ESIGI_USDT ($10M) — $500k gercek bir
            # bariyer degil (her an fiyata degiyor). Bu SADECE hedef kapisini besleyen
            # en_yakin_* hesabini etkiler.
            def _ciddi_duvarlar(kovalar):
                out = []
                for kf, d in kovalar.items():
                    mutabakat = len(d['borsalar'])
                    if d['usdt'] >= HEDEF_DUVAR_ESIGI_USDT and (mutabakat >= 2 or d['usdt'] >= HEDEF_DUVAR_ESIGI_USDT * 3):
                        out.append(kf)
                return out
            ciddi_ask = _ciddi_duvarlar(ask_kovalar)
            ciddi_bid = _ciddi_duvarlar(bid_kovalar)
            # v7.2: spread'e yapisik (<HEDEF_YAKIN_BOLGE_PCT) kovalar bariyer sayilmaz
            # — onlar defterin kendisi. Ilk YAPISAL bariyer aranir (aciklama sabitte).
            yakin_ask_sinir = anlik_fiyat * (1 + HEDEF_YAKIN_BOLGE_PCT / 100)
            yakin_bid_sinir = anlik_fiyat * (1 - HEDEF_YAKIN_BOLGE_PCT / 100)
            en_yakin_ask_fiyat = min((f for f in ciddi_ask if f > yakin_ask_sinir), default=0)
            en_yakin_bid_fiyat = max((f for f in ciddi_bid if 0 < f < yakin_bid_sinir), default=0)

            skor_girdi = {
                'fiyat': anlik_fiyat, 'bid_d': order_book_depth_bid_1pct,
                'ask_d': order_book_depth_ask_1pct, 'bnb_delta': bnb_delta,
                'byb_delta': byb_delta, 'okx_delta': okx_delta,
                'aktif_borsa': aktif_borsa_sayisi, 'vadeli_cvd': calculated_cvd,
                'spot_cvd': spot_cvd, 'oi': open_interest, 'funding': funding_rate,
                'bid_yas': en_olgun_yas_sn, 'ask_yas': en_olgun_ask_yas_sn,
                'likid': liquidation_pool_volume,
                'esik_d': esik_d, 'esik_l': esik_l,
                'esik_c_neg': esik_c_neg, 'esik_c_poz': esik_c_poz,
                # v5: surec baglami (trende karsi sinyal yasagi icin)
                'surec_rejim': durum.surec_rejim,
                'surec_tukenme': durum.surec_tukenme,
                'en_yakin_ask_fiyat': en_yakin_ask_fiyat,
                'en_yakin_bid_fiyat': en_yakin_bid_fiyat,
                # v7.3: TASFIYE AYRIMI girdileri — yon-bazli likidasyonun kendi
                # adaptif medyanina orani (pay: Coinalyze 5dk, payda: ayni kolonun
                # 7g sifir-olmayan medyani; birimler tutarli).
                'tasfiye_long_yogunluk': likidasyon_yogunlugu(agg_liq_long, esik_lik_long_med),
                'tasfiye_short_yogunluk': likidasyon_yogunlugu(agg_liq_short, esik_lik_short_med),
                'esik_volatilite': esik_vol,
                # v7.4: EMILIM AYRIMI girdileri (esik_spot + satici_tukenmesi seriden)
                'esik_spot_neg': esik_spot_neg,
            }
            # v7.4/v7.6: satici VE alici tukenmesi ozet dongusunde (seri_kopya
            # burada var; balina_skoru_hesapla'ya seri gecirmemek icin burada).
            # v7.8: tukenme 45dk'lik pencereden delta alir — pencere KARISIK
            # kaynakli ise (Coinalyze<->WS gecisi) delta gurultudur; olcum ATLANIR
            # (sonme=None). Kirli olcum, Faz-2 kararlarinin dayanacagi kohortu
            # zehirlemesin: bosluk > cop.
            if EMILIM_OLCUM_AKTIF and _cvd_kaynagi_tutarli(
                    seri_kopya, TUKENME_DILIM_SAYISI * TUKENME_DILIM_DK * 60):
                _sat_var, _sat_sonme = _akis_tukenmesi(
                    seri_kopya, 'SATIS', esik_c_neg, esik_spot_neg, esik_vol)
                _ali_var, _ali_sonme = _akis_tukenmesi(
                    seri_kopya, 'ALIS', esik_c_neg, esik_spot_neg, esik_vol)
            else:
                _sat_var = _ali_var = False
                _sat_sonme = _ali_sonme = None
            skor_girdi['satici_tukenmesi'] = _sat_var
            skor_girdi['sonme_orani'] = _sat_sonme
            skor_girdi['alici_tukenmesi'] = _ali_var          # v7.6
            skor_girdi['alici_sonme_orani'] = _ali_sonme
            # A) VERİ KALİTE KAPISI — kotu veriyle skor uretme
            kalite = veri_kalitesi_degerlendir(
                cvd_kaynak_saglikli=cvd_kaynak_saglikli,
                open_interest=open_interest,
                anlik_fiyat=anlik_fiyat,
                son_guncelleme_gecen=son_guncelleme_gecen,
                funding=funding_rate
            )

            # ---- v7.3: SUPURME DURUM MAKINESI (tespit + kayit; skora dokunmaz) ----
            # Kalite kapisi kapaliysa veya pencere yoksa HIC calistirilmaz
            # (spec §7.4: gecis yapilmaz, mevcut durumlar korunur).
            supurme_dip_aktif = False
            supurme_tepe_aktif = False
            supurme_yeni_onaylar = []   # [(yon, detay), ...] — kohort icin
            if SUPURME_TESPIT_AKTIF and kalite['cvd_guvenilir'] and pencere is not None:
                d_vadeli_p = pencere['d_vadeli_cvd']
                kap_dip = d_vadeli_p <= (esik_c_neg * KAPITULASYON_CARPANI)   # agresif satis
                kap_tepe = d_vadeli_p >= (esik_c_poz * KAPITULASYON_CARPANI)  # agresif alim
                tl_yog = skor_girdi['tasfiye_long_yogunluk'] >= TASFIYE_DIKEN_CARPANI
                ts_yog = skor_girdi['tasfiye_short_yogunluk'] >= TASFIYE_DIKEN_CARPANI
                # Fitil penceresi ARDISIK degerlendirmeleri bosluksuz DOSEMELI:
                # sabit 60sn, dongu suresi 60sn+govde oldugundan her turda birkac
                # saniyelik KOR aralik birakir (istisna turunda tam 60sn) -> fitil
                # kacar, KIRILMA supurme sanilir. Son degerlendirmeden bu yana
                # gecen sure kullanilir (tampon: +5sn, tavan: deque'in 15dk'si).
                son_eval = getattr(ozet_ve_analiz_dongusu, '_son_supurme_eval', 0.0)
                fitil_pencere_sn = min(900, max(60, (simdi_epoch - son_eval) + 5)) \
                    if son_eval > 0 else 60
                ozet_ve_analiz_dongusu._son_supurme_eval = simdi_epoch
                fitil_dip = durum.tick_min(fitil_pencere_sn)
                fitil_tepe = durum.tick_max(fitil_pencere_sn)
                akt_d, onay_d = supurme_takip_et(
                    durum.supurme_dip_durumlari, likidite_dipler, False,
                    anlik_fiyat, fitil_dip, esik_vol, kap_dip, tl_yog, simdi_epoch)
                akt_t, onay_t = supurme_takip_et(
                    durum.supurme_tepe_durumlari, likidite_tepeler, True,
                    anlik_fiyat, fitil_tepe, esik_vol, kap_tepe, ts_yog, simdi_epoch)
                supurme_dip_aktif = akt_d is not None
                supurme_tepe_aktif = akt_t is not None
                supurme_yeni_onaylar = [('LONG', o) for o in onay_d] + \
                                       [('SHORT', o) for o in onay_t]
                for yon_o, o in supurme_yeni_onaylar:
                    logging.info(
                        f"SUPURME ONAYLI ({yon_o}) -> seviye ${o['seviye']:,.0f} "
                        f"(test {o['test']}) | fitil ${o['fitil_uc']:,.0f} | "
                        f"delme {o['delme_pct']:.3f}% (vol %{esik_vol:.3f})")
            # ============ v7.5: EMİLİMİN YÖNÜ — ÖLÇÜM (Faz 1) ============
            # Absorbsiyon YÖN SÖYLEMEZ ("birisi emiyor" der, "kim ve neden" demez).
            skor_girdi['supurme_dip_aktif'] = supurme_dip_aktif
            skor_girdi['supurme_tepe_aktif'] = supurme_tepe_aktif

            # v7.5: SPOT DEFTER verisini skor_girdi ile tasi. balina_skoru_hesapla
            # bunlari _emilim_borsasi'na gecirir; emilim dict'i (skordan AYRI) doner.
            # Faz 1: skoru ETKILEMEZ — yalnizca olculur ve kohorta yazilir.
            skor_girdi['spot_bid_d'] = spot_bid_d
            skor_girdi['spot_ask_d'] = spot_ask_d
            skor_girdi['spot_ob_yasi_sn'] = spot_ob_yasi
            skor_girdi['spot_borsa_sayisi'] = spot_borsa_sayisi        # v7.6 mutabakat
            skor_girdi['spot_bid_agir_sayi'] = spot_bid_agir_sayi
            skor_girdi['spot_ask_agir_sayi'] = spot_ask_agir_sayi
            skor_girdi['perp_borsa_sayisi'] = perp_borsa_sayisi        # v7.7 perp mutabakat
            skor_girdi['perp_bid_agir_sayi'] = perp_bid_agir_sayi
            skor_girdi['perp_ask_agir_sayi'] = perp_ask_agir_sayi

            # v9.3 GOLGE: donus 9 eleman (golge_* SALT KAYIT — asagida yalniz
            # teshis UPDATE'ine yazilir, hicbir karar okumaz)
            (long_skor, short_skor, sinyal, rejim, aciklama, emilim,
             golge_yon, golge_kapi, golge_skor) = balina_skoru_hesapla(
                skor_girdi, pencere, kalite)

            # ---- v8: GRAB TEYIT PENCERESI icin dakikalik iz ----
            # anlik_kayit dict'i ZATEN gecmis_seri deque'sinde; buradaki ek alanlar
            # _grab_pencere_ozeti'nin 15dk penceresinde okunur. Skor yolu bu alanlari
            # OKUMAZ (Faz 1 fark=0 etkilenmez). Tukenme bayraklari HAM (None=olculemedi)
            # tasinir — sifir tuzagi: bool(None) ile False uydurulmaz.
            try:
                # Likidasyon TAZE degilse None yazilir: kesintide durum.agg_liq_* 0.0'da
                # donar; 0.0 "likidasyon yok" degil "olculemedi"dir (denetim bulgusu).
                _lik_taze = (time.time() - durum.coinalyze_liq_zaman) <= LIK_BAYATLIK_SN
                with durum.lock:
                    anlik_kayit.update({
                        'lik_long': agg_liq_long if _lik_taze else None,
                        'lik_short': agg_liq_short if _lik_taze else None,
                        'lik_long_yog': skor_girdi.get('tasfiye_long_yogunluk') if _lik_taze else None,
                        'lik_short_yog': skor_girdi.get('tasfiye_short_yogunluk') if _lik_taze else None,
                        'rejim': rejim,
                        'emici_yon': _emici_yon(emilim),
                        'alici_tuk': (emilim or {}).get('alici_tukenmesi'),
                        'satici_tuk': (emilim or {}).get('satici_tukenmesi'),
                    })
            except Exception as e:
                logging.warning(f"grab dakika izi yazilamadi (akis devam eder): {e}")

            # v5 COOLDOWN: sinyal cikti ama son 30dk icinde zaten sinyal verildiyse
            # sustur. Ayni hareket 6 kez sinyallenmez — balina bir kez konusur.
            if sinyal in ("LONG", "SHORT"):
                simdi_cd = time.time()
                if (simdi_cd - durum.son_sinyal_zamani) < SINYAL_COOLDOWN_SN:
                    aciklama += f" COOLDOWN({(SINYAL_COOLDOWN_SN-(simdi_cd-durum.son_sinyal_zamani))/60:.0f}dk kaldi)"
                    sinyal = "BEKLE"
                else:
                    durum.son_sinyal_zamani = simdi_cd
                    durum.son_sinyal_yonu = sinyal

            long_skor = round(long_skor, 1)
            short_skor = round(short_skor, 1)
            guven_skoru = max(long_skor, short_skor)

            # ---- v4: SÜREÇ HAFIZASI (dagitim/toplama olgunlugu + tukenme) ----
            surec = surec_takip_et(
                durum, rejim, anlik_fiyat, spot_cvd, calculated_cvd,
                order_book_depth_bid_1pct, order_book_depth_ask_1pct,
                en_olgun_yas_sn, en_olgun_ask_yas_sn, funding_rate,
                agg_liq_long, agg_liq_short, seri_kopya
            )
            # Paneli besleyecek surec durumunu ayarlar tablosuna yaz (sema degismez)
            # v5: her dakika DEGIL, sadece durum degistiginde yaz (gereksiz yuk yok)
            surec_imza = f"{surec['surec_rejim']}|{surec['tukenme']}|{int(surec['olgunluk']*10)}|{int(surec['sure_dk']/10)}"
            if surec_imza != getattr(ozet_ve_analiz_dongusu, "_son_surec_imza", ""):
                ozet_ve_analiz_dongusu._son_surec_imza = surec_imza
                try:
                    _ayarlar_yaz("surec_durumu", {
                        "rejim": surec["surec_rejim"], "sure_dk": surec["sure_dk"],
                        "olgunluk": surec["olgunluk"], "tukenme": surec["tukenme"],
                        "tukenme_detay": surec["tukenme_detay"], "uyari": surec["uyari"],
                        "zirve_fiyat": round(durum.surec_zirve_fiyat, 1),
                        "dip_fiyat": round(durum.surec_dip_fiyat, 1),
                        "baslangic_spotcvd": round(durum.surec_baslangic_spotcvd, 0),
                        "anlik_spotcvd": round(spot_cvd, 0),
                        "guncelleme": datetime.datetime.utcnow().isoformat()
                    })
                except Exception as e:
                    logging.warning(f"Surec durumu yazma hatasi: {e}")

            # ---- v5.2: UC-BORSALI LIKIDITE HARITASI ----
            # Tek borsa yerine BNB+BYB+OKX birlesik kovalardan en kalin duvarlar.
            # 'borsa' alani = o seviyede kac borsada duvar var (mutabakat).
            # 3 borsada birden duvar = gercek seviye; 1 borsada = spoof suphesi.
            def _harita_yap(kovalar, tersine):
                items = sorted(kovalar.items(), key=lambda x: -x[1]['usdt'])[:3]
                out = []
                for kfiyat, d in items:
                    out.append({
                        "fiyat": round(kfiyat, 1),
                        "usdt": round(d['usdt'], 0),
                        "borsa": len(d['borsalar']),
                        "kaynak": "+".join(sorted(d['borsalar']))
                    })
                return out

            likidite_bid_json = _harita_yap(bid_kovalar, False)
            likidite_ask_json = _harita_yap(ask_kovalar, True)

            # ---- v6: VE-KAPISI RED SEBEPLERİ (kumulatif sayac) ----
            # Skor esigi gecti ama kapi reddettiyse, HANGI kapi reddetti?
            # Iki hafta sonra "hangi kapi cok siki" sorusunu KANITLA cevaplayacagiz.
            # Log silinir, veritabani kalir -> ayarlar tablosuna yaziyoruz.
            if "VE-RED:" in aciklama:
                try:
                    red_kismi = aciklama.split("VE-RED:")[1].split()[0]
                    if not hasattr(ozet_ve_analiz_dongusu, "_ve_red_sayac"):
                        ozet_ve_analiz_dongusu._ve_red_sayac = {}
                        ozet_ve_analiz_dongusu._ve_red_toplam = 0
                        ozet_ve_analiz_dongusu._ve_red_yazim = 0
                    for kapi in red_kismi.split(","):
                        kapi = kapi.strip()
                        if kapi:
                            ozet_ve_analiz_dongusu._ve_red_sayac[kapi] = \
                                ozet_ve_analiz_dongusu._ve_red_sayac.get(kapi, 0) + 1
                    ozet_ve_analiz_dongusu._ve_red_toplam += 1
                    # 10 redde bir yaz (gereksiz Supabase yuku olmasin)
                    if ozet_ve_analiz_dongusu._ve_red_toplam - ozet_ve_analiz_dongusu._ve_red_yazim >= 10:
                        ozet_ve_analiz_dongusu._ve_red_yazim = ozet_ve_analiz_dongusu._ve_red_toplam
                        _ayarlar_yaz("ve_kapisi_redleri", {
                            "toplam_red": ozet_ve_analiz_dongusu._ve_red_toplam,
                            "kapi_sayaclari": dict(ozet_ve_analiz_dongusu._ve_red_sayac),
                            "guncelleme": datetime.datetime.utcnow().isoformat(),
                            "not": "Skor 90'i gecti ama bu kapilar reddetti. Yuksek sayi = kapi cok siki olabilir."
                        })
                except Exception as e:
                    logging.warning(f"VE-RED sayac hatasi: {e}")

            # ---- SUPABASE'E YAZ (sema AYNI, sadece degerler artik anlamli) ----
            payload = {
                "anlik_fiyat": anlik_fiyat,
                "spot_cvd": spot_cvd,
                "vadeli_cvd": calculated_cvd,
                "open_interest": open_interest,
                "funding_rate": funding_rate,
                "order_book_depth_bid_1pct": order_book_depth_bid_1pct,
                "order_book_depth_ask_1pct": order_book_depth_ask_1pct,
                "liquidation_pool_volume": liquidation_pool_volume,
                "agg_long_liq": agg_liq_long,
                "agg_short_liq": agg_liq_short,
                "long_short_ratio": agg_ls_ratio,
                "en_olgun_emir_yasi_sn": en_olgun_yas_sn,
                "en_olgun_ask_yasi_sn": en_olgun_ask_yas_sn,
                "long_skor": long_skor,
                "short_skor": short_skor,
                "guven_skoru": guven_skoru,
                # v8.1: scalp SUSTURULDU -> DB'ye yazilan aktif sinyal BEKLE. balina_skoru_
                # hesapla ('sinyal') DEGISMEDI (Faz 1 fark=0); long_skor/short_skor + kohort
                # KAYITTA kalir (scalp referansi korunur). Swing karari ayri anahtarda.
                "sinyal_durumu": (sinyal if SCALP_SINYAL_AKTIF else "BEKLE"),
                "yakin_likidite_bid": likidite_bid_json,
                "yakin_likidite_ask": likidite_ask_json,
                # v7.10: EMILIM ARSIVI — dakikalik KALICI kayit (balina_avcisi_data).
                # GEREKCE: canli_emilim (balina_ayarlar) her dakika UZERINE yazilir ->
                # gecmis YOK. Faz 1'in TAMAMI "iki hafta veri" uzerine kurulu; arsiv
                # olmadan DIP_TOPLAMA_SPOT vs _TEYITSIZ ayrimi POST-HOC sorgulanamaz.
                # Skoru ETKILEMEZ (olcum-only). Kolonlar NULLABLE — olculemeyen dakikada
                # None yazilir, 0 DEGIL (spec §9.2 sifir tuzagi). spot/perp_ob_yasi_sn
                # dogrudan dongu degiskeninden (emilim dict'inde yok); ikisi de 2976/2984'te
                # KOSULSUZ init edildi -> payload'da her zaman tanimli (NameError yok).
                "emilim_borsasi":     (emilim or {}).get('emilim_borsasi'),
                "spot_egilim":        (emilim or {}).get('spot_egilim'),
                "perp_egilim":        (emilim or {}).get('perp_egilim'),
                "emilim_spot_pay":    (emilim or {}).get('emilim_spot_pay'),
                "emilim_esnekligi":   (emilim or {}).get('emilim_esnekligi'),
                "satici_tukenmesi":   (emilim or {}).get('satici_tukenmesi'),
                "sonme_orani":        (emilim or {}).get('sonme_orani'),
                "alici_tukenmesi":    (emilim or {}).get('alici_tukenmesi'),
                "alici_sonme_orani":  (emilim or {}).get('alici_sonme_orani'),
                "spot_bid_agir_sayi": (emilim or {}).get('spot_bid_agir_sayi'),
                "spot_ask_agir_sayi": (emilim or {}).get('spot_ask_agir_sayi'),
                "spot_borsa_sayisi":  (emilim or {}).get('spot_borsa_sayisi'),
                "perp_bid_agir_sayi": (emilim or {}).get('perp_bid_agir_sayi'),
                "perp_ask_agir_sayi": (emilim or {}).get('perp_ask_agir_sayi'),
                "perp_borsa_sayisi":  (emilim or {}).get('perp_borsa_sayisi'),
                "spot_ob_yasi_sn":    round(spot_ob_yasi, 1) if spot_ob_yasi is not None else None,
                "perp_ob_yasi_sn":    round(perp_ob_yasi, 1) if perp_ob_yasi is not None else None,
            }

            if anlik_fiyat > 0:
                # v7.3: insert kendi try'inda — gecici Supabase hatasi (503/timeout),
                # ayni dakika ONAYLI'ya gecen supurmenin kohort kaydini KACIRAMAZ
                # (rising-edge makinede tuketildi; olay bir daha uretilmez).
                yeni_satir_id = None
                try:
                    ins = supabase.table("balina_avcisi_data").insert(payload).execute()
                    if ins.data:
                        yeni_satir_id = ins.data[0].get('id')
                except Exception as e:
                    logging.warning(f"Veri enjekte hatasi (kohort akisi devam eder): {e}")

                # ---- v7.9: CANLI EMILIM anlik goruntusu ----
                # "Su an emen kim?" sorusu bugune kadar HICBIR YERDEN okunamiyordu:
                # emilim_borsasi yalnizca tasfiye/supurme OLAYI aninda kohorta
                # yaziliyordu; dakikalik tabloda yok (sema sabit). balina_ayarlar
                # zaten key-value JSONB ve panel zaten okuyor -> sifir migrasyon.
                # Skoru ETKILEMEZ — sadece gozlemlenebilirlik. (_ayarlar_yaz kendi
                # hatasini yutar; akis kesilmez.)
                _ayarlar_yaz('canli_emilim', {
                    'guncelleme': datetime.datetime.utcnow().isoformat(),
                    'emilim_borsasi': (emilim or {}).get('emilim_borsasi'),
                    'spot_egilim': (emilim or {}).get('spot_egilim'),
                    'perp_egilim': (emilim or {}).get('perp_egilim'),
                    'emilim_esnekligi': (emilim or {}).get('emilim_esnekligi'),
                    'satici_tukenmesi': (emilim or {}).get('satici_tukenmesi'),
                    'sonme_orani': (emilim or {}).get('sonme_orani'),
                    'alici_tukenmesi': (emilim or {}).get('alici_tukenmesi'),
                    'alici_sonme_orani': (emilim or {}).get('alici_sonme_orani'),
                    'spot_borsa_sayisi': (emilim or {}).get('spot_borsa_sayisi'),
                    'spot_bid_agir_sayi': (emilim or {}).get('spot_bid_agir_sayi'),
                    'spot_ask_agir_sayi': (emilim or {}).get('spot_ask_agir_sayi'),
                    'perp_borsa_sayisi': (emilim or {}).get('perp_borsa_sayisi'),
                    'perp_bid_agir_sayi': (emilim or {}).get('perp_bid_agir_sayi'),
                    'perp_ask_agir_sayi': (emilim or {}).get('perp_ask_agir_sayi'),
                    'spot_ob_yasi_sn': round(spot_ob_yasi, 1) if spot_ob_yasi is not None else None,
                    'perp_ob_yasi_sn': round(perp_ob_yasi, 1) if perp_ob_yasi is not None else None,
                    'cvd_kaynak': cvd_kaynak_etiketi,
                })

                # ---- v7.3: TASFIYE KOHORTU — 2x2 faktoriyel olay kaydi ----
                # A: zorla kapatma dikeni + OI dususu (v7.3.1: HAM OLGUDAN —
                #    rejim adina bagli etiket, kanonik supurmenin geri-alim
                #    barini yanlis hucreye dusuruyor, saf tasfiyeyi kaciriyordu)
                # B: taze supurme onayi. Ikisi ayni dakikada catisirsa TEK kayit,
                # iki bayrakla — iki hafta sonra hangi hucrenin kenar urettigi okunur.
                d_oi_k = pencere['d_oi_pct'] if pencere else 0.0
                tasfiye_a, tasfiye_yonu = _tasfiye_bayraklari(
                    skor_girdi['tasfiye_long_yogunluk'],
                    skor_girdi['tasfiye_short_yogunluk'], d_oi_k)

                # ========== v8.1 FAZ B: KADEMELI SWING MOTORU (scalp'tan AYRI) ==========
                # Uc sensor (grab=supurme durumu, tasfiye, emici) + swing seviye haritasi
                # -> kademeli karar. Skoru ETKILEMEZ; balina_ayarlar['swing_karar']'a
                # yazilir. Hata akisi kesmez. (durum.swing_seviyeler 10dk'da yenilenir.)
                if SWING_MOTOR_AKTIF:
                    try:
                        _sw = durum.swing_seviyeler or []
                        # v8.7: grab ozeti artik SAF _grab_ozeti'den (denetim 2/2 KESIN:
                        # SILAHLI baslad sayilmaz + ONAYLI'ya tazelik suzgeci).
                        _grab = _grab_ozeti(durum.supurme_dip_durumlari,
                                            durum.supurme_tepe_durumlari, time.time())
                        _kademe = _swing_kademe(anlik_fiyat, _sw, esik_vol, _grab,
                                                bool(tasfiye_a), emilim, funding_rate, d_oi_k)
                        _hedef = None
                        # v8.8-D: _hedef HAZIRLAN'da da hesaplanir (SADECE KAYIT —
                        # 934 satirin 933'unde swing_rr None'di; "R/R gecer miydi?"
                        # geriye donuk cevapsizdi). SINYAL'e TERFI SARTI BIREBIR AYNI:
                        # 4/4 (dort_dort) VE _hedef['gecerli']; R/R vetosu SINYAL'i
                        # HAZIRLAN'a dusurme davranisi aynen korunur. HAZIRLAN'da
                        # gecersiz R/R hicbir seyi degistirmez, yalnizca kaydedilir.
                        if _kademe['kademe'] in ('HAZIRLAN', 'SINYAL') and _kademe['yon']:
                            # v8.7: 'gizli' filtresi KALDIRILDI — gizleme yalniz panel
                            # gosterimi icindir (A3 birlestirme); en yogun likidasyon
                            # kumesi bir HL/VP ile cakisti diye MIKNATIS olmaktan cikmaz.
                            _mag = max((s for s in _sw if s.get('kaynak') == 'LIQ'),
                                       key=lambda s: s.get('hacim', 0), default=None)
                            _hedef = _swing_hedef_stop(_kademe['yon'], anlik_fiyat, _sw,
                                                       esik_vol, _mag['fiyat'] if _mag else None)
                            if _kademe['kademe'] == 'SINYAL' and not _hedef['gecerli']:
                                # R/R vetosu -> SINYAL degil, HAZIRLAN (davranis AYNI)
                                _kademe['kademe'] = 'HAZIRLAN'
                                _kademe['sebepler'].append(f"R/R yetersiz ({_hedef['sebep']})")
                        durum.swing_karar = {**_kademe, 'hedef_stop': _hedef}
                        # swing_tetik: hangi sensorler aktif (SEVIYE hep var — buraya
                        # geldiyse seviyeye yakin). GRAB/TASFIYE/EMILIM eklenir.
                        _st = ['SEVIYE']
                        if _grab.get('baslad'): _st.append('GRAB')
                        if tasfiye_a: _st.append('TASFIYE')
                        if _kademe['sartlar'].get('emici_yon'): _st.append('EMILIM')
                        _tetik = '+'.join(_st)
                        _ys = _kademe.get('yakin_seviye') or {}
                        _hs = _hedef or {}
                        _ayarlar_yaz('swing_karar', {
                            'guncelleme': datetime.datetime.utcnow().isoformat(),
                            'anlik_fiyat': round(anlik_fiyat, 1),
                            'vol_pct': round(esik_vol, 4) if esik_vol else None,
                            'tetik': _tetik, **_kademe, 'hedef_stop': _hedef})
                        # ---- C1: DAKIKALIK ARSIV -> balina_avcisi_data (UPDATE, best-effort) ----
                        # Payload'a EKLEMIYORUZ: SQL kolonlari yoksa ANA insert olmesin diye
                        # ayri UPDATE. Kolon yoksa gracefully atlar; core veri her halukarda yazildi.
                        if SWING_ARSIV_AKTIF and yeni_satir_id:
                            try:
                                supabase.table("balina_avcisi_data").update({
                                    "swing_kademe": _kademe['kademe'],
                                    "swing_yon": _kademe['yon'],
                                    "swing_skor": _kademe['kademe_skoru'],
                                    "swing_seviye": _ys.get('fiyat'),
                                    "swing_kisa_hedef": _hs.get('kisa_hedef'),
                                    "swing_uzun_hedef": _hs.get('swing_hedef'),
                                    "swing_stop": _hs.get('stop'),
                                    "swing_rr": _hs.get('rr_swing'),
                                    "swing_rr_kisa": _hs.get('rr_kisa'),   # v8.9-A: SADECE KAYIT, kapi DEGIL
                                    "swing_tetik": _tetik,
                                }).eq("id", yeni_satir_id).execute()
                            except Exception as e:
                                logging.warning(f"swing arsiv UPDATE hatasi (kolonlar ALTER edildi mi?): {e}")
                        # ---- C2: SWING KOHORTU (rising-edge: SINYAL'e GECISTE bir kez) ----
                        # v8.7: rising-edge'e YON-BAZLI 600sn cooldown eklendi (tasfiye
                        # kohortundaki korumanin aynisi): SINYAL bir dakikalik sensor
                        # titremesiyle dusup geri gelirse ayni kurulum COKLU yazilmasin
                        # (geri-test n'ini sisiriyordu — denetim bulgusu).
                        if (_kademe['kademe'] == 'SINYAL' and _hedef and _hedef.get('gecerli')
                                and durum.swing_son_kademe != 'SINYAL'
                                and (time.time() - durum.son_swing_kohort_ts.get(_kademe['yon'], 0)) >= 600):
                            try:
                                # v8.7 RMW korumasi: okuma HATASI 'bos kohort' DEGILDIR —
                                # eski kod gecici 503'te tum olay gecmisini bos listeyle
                                # ezebilirdi (kullanici zaten bir veri kaybi yasadi).
                                _ok_kk, _kk = _ayarlar_oku_katilim('swing_kohortu')
                                if not _ok_kk:
                                    raise RuntimeError('kohort okunamadi — bu tur yazim atlandi (veri korunur)')
                                _olylar = []
                                if _kk and _kk.get('deger'):
                                    _olylar = (_kk['deger'].get('olaylar', [])
                                               if isinstance(_kk['deger'], dict) else [])
                                _olylar.append({
                                    'zaman': datetime.datetime.utcnow().isoformat(),
                                    'yon': _kademe['yon'], 'giris': round(anlik_fiyat, 1),
                                    'kisa_hedef': _hs.get('kisa_hedef'), 'swing_hedef': _hs.get('swing_hedef'),
                                    'stop': _hs.get('stop'), 'rr_swing': _hs.get('rr_swing'),
                                    'skor': _kademe['kademe_skoru'], 'seviye': _ys.get('fiyat'),
                                    'tetik': _tetik})
                                # v9.4: budama gercek sinyalleri korur (once ADAY atilir)
                                _olylar = _kohort_buda(_olylar, KOHORT_AZAMI_KAYIT)
                                _ayarlar_yaz('swing_kohortu', {
                                    'guncelleme': datetime.datetime.utcnow().isoformat(),
                                    'olaylar': _olylar})
                                durum.son_swing_kohort_ts[_kademe['yon']] = time.time()
                                logging.info(f"SWING KOHORT: yeni SINYAL olayi ({_kademe['yon']}) "
                                             f"-> toplam {len(_olylar)}")
                            except Exception as e:
                                logging.warning(f"swing kohort yazma hatasi: {e}")
                        durum.swing_son_kademe = _kademe['kademe']
                        if _kademe['kademe'] in ('HAZIRLAN', 'SINYAL'):
                            # v9.2: kapi artik rr_kisa — log her ikisini gosterir
                            logging.info(f"SWING {_kademe['kademe']} {_kademe['yon'] or ''} "
                                         f"skor={_kademe['kademe_skoru']} "
                                         f"{'RR_kisa=' + str(_hedef['rr_kisa']) + ' RR_swing=' + str(_hedef['rr_swing']) if _hedef else ''}")
                    except Exception as e:
                        logging.warning(f"swing motor hatasi (akis devam eder): {e}")

                # ========== v8: LIQ GRAB SWING MOTORU (ADIM 2-5; 15dk KAPALI mum) ==========
                # Her turda "yeni kapanmis 15dk mumu var mi?" sorulur — ayni mum iki kez
                # ISLENMEZ (durum.son_islenen_15dk_ts). Aday (ADIM 2) -> kapanis karari
                # (ADIM 3, AYNI kapali mumda) -> order flow teyidi (ADIM 4) -> kagit ustu
                # sinyal + kayit (ADIM 5). Scalp skor yoluna DOKUNMAZ; hata akisi kesmez.
                _grab_teshis_dk = None   # v8.8-F: bu dakikada aday olustuysa teshisi (kolonlara)
                if SWING_MOTOR_AKTIF:
                    try:
                        with durum.lock:
                            _mumlar = list(durum.mumlar_15dk)
                        _simdi_g = time.time()
                        _kapali15 = [m for m in _mumlar if m['t'] + 900 <= _simdi_g]
                        _mum15 = _kapali15[-1] if _kapali15 else None
                        if _mum15 is not None and _mum15['t'] != durum.son_islenen_15dk_ts:
                            durum.son_islenen_15dk_ts = _mum15['t']
                            # ATR15: sweep mumundan ONCEKI 14 kapali mum — mumun kendi
                            # araligi kendi delme esigini sisirmesin
                            _onceki15 = _kapali15[:-1]
                            _atr = _atr15(_onceki15)
                            _h20 = [m['v'] for m in _onceki15[-20:] if m.get('v')]
                            _hacim_ort = (sum(_h20) / len(_h20)) if len(_h20) >= 20 else None
                            _poz = _grab_pencere_ozeti(seri_kopya, _mum15['t'], _mum15['t'] + 900)
                            # v8.8-B: faz teshisi icin pencere kayitlari (ayni dilim)
                            _pkayit = [r for r in seri_kopya
                                       if _mum15['t'] <= (r.get('ts') or 0) < _mum15['t'] + 900]
                            _svl = durum.swing_seviyeler or []
                            for _k in list(durum.grab_cooldown):   # budama (2x cooldown)
                                if _simdi_g - durum.grab_cooldown[_k] > SWEEP_COOLDOWN_DK * 120:
                                    del durum.grab_cooldown[_k]
                            _adaylar = []
                            # cooldown YAKINLIK ile eslesir (denetim: adaptif yenilemede
                            # VP/LIQ kume fiyati birkac dolar kayar; tam-anahtar esleme
                            # 90dk yasagi sessizce deliyordu). Band: SEVIYE_KUMELEME_VOL
                            # x vol — "ayni seviye" tanimiyla birebir.
                            _cd_band = SEVIYE_KUMELEME_VOL * (esik_vol or 0.0)
                            for _s in _svl:
                                if _s.get('gizli') or (_s.get('guc') or 0) < SWING_SEVIYE_MIN_GUC:
                                    continue          # ADIM 1: zayif seviyede grab CALISMAZ
                                _sf = _s['fiyat']
                                _cd_ts = None
                                for _ck, _cts in durum.grab_cooldown.items():
                                    if (_ck == round(_sf) or (_cd_band > 0 and _sf > 0 and
                                            abs(_ck - _sf) / _sf * 100.0 <= _cd_band)):
                                        _cd_ts = max(_cd_ts or 0.0, _cts)
                                _aday = _sweep_adayi(_mum15, _sf, _s.get('guc'), _atr,
                                                     _hacim_ort, _poz.get('lik_toplam'),
                                                     _cd_ts, _simdi_g)
                                if _aday:
                                    # pencere EKSIKSE cooldown tuketilmez (denetim: restart
                                    # sonrasi ilk mumun "kor" adayi, 90dk boyunca ayni
                                    # seviyedeki GERCEK sweep'i kilitliyordu)
                                    if not _poz.get('eksik'):
                                        durum.grab_cooldown[round(_sf)] = _simdi_g
                                    _aday['teyit'] = _sweep_teyit(_aday['yon'],
                                                                  _aday['kapanis_tipi'], _poz)
                                    # v8.8-B: teshis alanlari (SADECE KAYIT — ham'a gider)
                                    _aday['teshis'].update(_grab_teshis(
                                        _aday, _s, _poz, _pkayit,
                                        esik_lik_long_med, esik_lik_short_med, _simdi_g))
                                    _adaylar.append(_aday)
                            if _adaylar:   # v8.8-F: kolonlara en guclu adayin teshisi gider
                                _grab_teshis_dk = max(_adaylar,
                                                      key=lambda a: a.get('sweep_guc') or 0)['teshis']
                            _fvg = _fvg_bul(_kapali15)   # G1: SADECE KAYIT — girisi degistirmez
                            _mum_kapanis_iso = datetime.datetime.utcfromtimestamp(
                                _mum15['t'] + 900).isoformat()
                            _yeni_olaylar = []
                            _sinyal_karti = None
                            # ---- v8.8-C: onceki mumun DEVAM/None adaylari icin N+1
                            # kapanis olcumu (paralel kohort; SINYAL URETMEZ, cooldown'a
                            # DOKUNMAZ — ayni adaydan turer). Liste tek mum yasar.
                            for _o in _grab_n1_kayitlari(durum.grab_n1_bekleyen, _mum15, _poz):
                                _o['zaman'] = _mum_kapanis_iso
                                _yeni_olaylar.append(_o)
                            durum.grab_n1_bekleyen = []
                            if _adaylar:
                                for _a in _adaylar:
                                    _a['fvg'] = _fvg     # ham'a gider (ham = aday dict'i)
                                # v8.8-C: DEVAM/None siniflanan adaylar N+1 olcumune girer
                                # (tek mum omru; DONUS zaten ayni mumda siniflandi)
                                durum.grab_n1_bekleyen = [
                                    {'seviye': a['sweep_seviye'], 'yon': a['yon'],
                                     'mum_ts': a['mum_ts'], 'ham': a}
                                    for a in _adaylar if a['kapanis_tipi'] != 'DONUS']
                                _sinyalli = [a for a in _adaylar if a['teyit'].get('sonuc')]
                                # ayni mumda birden cok teyitli aday olursa EN GUCLU seviye
                                # sinyal olur; digerleri aday olarak kayda gecer
                                _secilen = max(_sinyalli, key=lambda a: a.get('sweep_guc') or 0) \
                                    if _sinyalli else None
                                for _a in _adaylar:
                                    if _a is _secilen:
                                        continue      # sinyal kaydi asagida (hedef/stop ile)
                                    # ADIM 2 kaydi: aday != sinyal; hedef/stop/rr ANAHTARLARI
                                    # bilerek YOK — _swing_backtest bunlari sinyal sanmasin
                                    _yeni_olaylar.append({
                                        'zaman': _mum_kapanis_iso, 'tetik': 'GRAB_ADAY',
                                        'seviye': _a['sweep_seviye'], 'skor': _a['sweep_guc'],
                                        'ham': _a})
                                if _secilen is not None:
                                    _tip = _secilen['teyit']['sonuc']
                                    # islem yonu: DONUS = sweep etiketiyle ayni;
                                    # DEVAM = kirilim yonu (etiketin tersi)
                                    _iy = _secilen['yon'] if _tip == 'GRAB_DONUS' else \
                                        ('LONG' if _secilen['yon'] == 'SHORT' else 'SHORT')
                                    _giris = _secilen['kapanis']   # teyit anindaki 15dk kapanisi
                                    _stp = _grab_stop(_tip, _secilen['yon'], _secilen['fitil_ucu'],
                                                      _secilen['sweep_seviye'], _giris, _atr)
                                    _hs2 = _swing_hedef_stop(_iy, _giris, _svl, esik_vol,
                                                             stop_zorla=_stp,
                                                             min_guc=SWING_SEVIYE_MIN_GUC) \
                                        if _stp is not None else None
                                    _ham = dict(_secilen)
                                    _ham['hedef_stop'] = _hs2
                                    if _hs2 and _hs2['gecerli']:
                                        _yeni_olaylar.append({
                                            'zaman': _mum_kapanis_iso, 'yon': _iy,
                                            'giris': _giris,
                                            'kisa_hedef': _hs2['kisa_hedef'],
                                            'swing_hedef': _hs2['swing_hedef'],
                                            'stop': _hs2['stop'], 'rr_swing': _hs2['rr_swing'],
                                            'skor': _secilen['sweep_guc'],
                                            'seviye': _secilen['sweep_seviye'],
                                            'tetik': _tip, 'ham': _ham})
                                        _sinyal_karti = {
                                            'zaman': _mum_kapanis_iso, 'tip': _tip, 'yon': _iy,
                                            'giris': _giris, 'seviye': _secilen['sweep_seviye'],
                                            'fitil_ucu': _secilen['fitil_ucu'],
                                            'stop': _hs2['stop'],
                                            'kisa_hedef': _hs2['kisa_hedef'],
                                            'swing_hedef': _hs2['swing_hedef'],
                                            'rr_kisa': _hs2['rr_kisa'],
                                            'rr_swing': _hs2['rr_swing'],
                                            'guc': _secilen['sweep_guc'],
                                            'teyit': _secilen['teyit']}
                                        logging.info(
                                            f"GRAB SINYAL {_tip} {_iy} @ {_giris:,.1f} | "
                                            f"seviye {_secilen['sweep_seviye']:,.1f} "
                                            f"(guc {_secilen['sweep_guc']}) | "
                                            f"stop {_hs2['stop']:,.1f} | hedef "
                                            f"{_hs2['kisa_hedef']:,.1f}/{_hs2['swing_hedef']:,.1f} | "
                                            f"rr_kisa {_hs2['rr_kisa']}")
                                        if SWING_ARSIV_AKTIF and yeni_satir_id:
                                            try:
                                                supabase.table("balina_avcisi_data").update({
                                                    "swing_kademe": 'SINYAL', "swing_yon": _iy,
                                                    "swing_skor": _secilen['sweep_guc'],
                                                    "swing_seviye": _secilen['sweep_seviye'],
                                                    "swing_kisa_hedef": _hs2['kisa_hedef'],
                                                    "swing_uzun_hedef": _hs2['swing_hedef'],
                                                    "swing_stop": _hs2['stop'],
                                                    "swing_rr": _hs2['rr_swing'],
                                                    "swing_tetik": _tip,
                                                }).eq("id", yeni_satir_id).execute()
                                            except Exception as e:
                                                logging.warning(f"grab arsiv UPDATE hatasi: {e}")
                                    else:
                                        # ADIM 5 R/R kapisi: sinyal URETILMEZ, kayit kalir.
                                        # ust-duzey hedef/stop anahtari YOK (backtest korumasi).
                                        # rr_red YALNIZ gercek R/R reddi (denetim: 'yapisal
                                        # seviye eksik' reddini rr_red saymak kalibrasyon
                                        # SQL'ini kirletiyordu); sebep ayrica kaydedilir.
                                        _ham['rr_red'] = bool(_hs2 and _hs2.get('rr_kisa') is not None
                                                              and not _hs2.get('gecerli'))
                                        _ham['red_sebebi'] = (_hs2 or {}).get('sebep',
                                                                              'stop hesaplanamadi')
                                        _yeni_olaylar.append({
                                            'zaman': _mum_kapanis_iso, 'tetik': 'GRAB_ADAY',
                                            'seviye': _secilen['sweep_seviye'],
                                            'skor': _secilen['sweep_guc'], 'ham': _ham})
                                        logging.info(
                                            f"GRAB {_tip} teyitli ama sinyal YOK: "
                                            f"{(_hs2 or {}).get('sebep', 'stop hesaplanamadi')}")
                            # kohort yazimi — TEK RMW (v8.7 korumasi: okuma hatasi ->
                            # 'bos kohort' DEGIL, bu tur yazim atlanir, veri korunur).
                            # G2: her yeni 15dk mumunda bekleyen olaylarin CHoCH'u da
                            # burada olgunlasir (ayri okuma/yazma YOK — ayni RMW).
                            # Denetim: tampon (grab_kohort_bekleyen) — dakika satiri
                            # SINYAL'e UPDATE edildikten sonra gecici Supabase hatasi
                            # olayi kalici dusuremez; sonraki mumda yeniden denenir.
                            _tum_yeni = list(durum.grab_kohort_bekleyen) + _yeni_olaylar
                            _ok_kk, _kk = _ayarlar_oku_katilim('swing_kohortu')
                            if not _ok_kk:
                                durum.grab_kohort_bekleyen = _tum_yeni
                                raise RuntimeError('kohort okunamadi — olaylar tamponda, sonraki mumda denenir')
                            _oly = []
                            if _kk and _kk.get('deger'):
                                _oly = (_kk['deger'].get('olaylar', [])
                                        if isinstance(_kk['deger'], dict) else [])
                            _choch_degisti = _choch_olgunlastir(_oly, _kapali15, _mum15['t'])
                            _oly.extend(_tum_yeni)
                            if _choch_degisti or _tum_yeni:
                                # v9.4: budama gercek sinyalleri korur (once ADAY atilir)
                                _oly = _kohort_buda(_oly, KOHORT_AZAMI_KAYIT)
                                durum.grab_kohort_bekleyen = _tum_yeni   # yazim oncesi koru
                                _ayarlar_yaz('swing_kohortu', {
                                    'guncelleme': datetime.datetime.utcnow().isoformat(),
                                    'olaylar': _oly})
                                durum.grab_kohort_bekleyen = []          # dogrulanmis yazim
                                logging.info(f"GRAB KOHORT: +{len(_tum_yeni)} yeni | "
                                             f"choch_guncellendi={_choch_degisti} -> "
                                             f"toplam {len(_oly)}")
                            if _sinyal_karti:
                                _ayarlar_yaz('grab_aktif_sinyal', _sinyal_karti)
                    except Exception as e:
                        logging.warning(f"grab motoru hatasi (akis devam eder): {e}")

                # ---- v8.8-F: TESHIS KOLONLARI (dakikalik; ayri UPDATE — mevcut swing
                # arsiv deseninin aynisi: kolon yoksa ana insert ASLA olmez). seviye_*
                # her dakika yakin seviyeden; aday-duzeyi alanlar yalniz o dakikada
                # aday olustuysa dolar (yoksa NULL — sifir uydurulmaz). ----
                if SWING_ARSIV_AKTIF and yeni_satir_id:
                    try:
                        _ys8 = (durum.swing_karar or {}).get('yakin_seviye') or {}
                        _igt8 = _ys8.get('ilk_gorulme_ts')
                        _td8 = _grab_teshis_dk or {}
                        # ---- v9.0-B: acik tutarsizlik IZI (duzeltme YOK — sebep
                        # bilinmeden duzeltmek olmayan hatayi duzeltmek olur).
                        # seviye_guc NULL yazilirken swing_seviye doluysa logla;
                        # bir gun sonra log okunur, sebep bulunur, spec ondan sonra.
                        try:
                            if _ys8 == {} and _ys.get('fiyat'):
                                logging.info("TESHIS: yakin_seviye bos ama swing_seviye dolu "
                                             f"(id={yeni_satir_id}) — v9.0-B izi")
                        except NameError:
                            pass   # _ys tanimsiz (kademe blogu erken dustu) — iz atlanir
                        # ---- v9.0-A: HARITA OZETI (uc alan; SALT KAYIT, kapi DEGIL).
                        # Dakikalik dongude durum.swing_seviyeler'den DOGRUDAN hesap
                        # (yenileme aninda kopyalamak medyan yasi dondururdu). Harita
                        # bos/None ise ucu de None — "sifir seviye var" degil "harita
                        # henuz kurulmadi" (v8.9-B sifir tuzaginin aynisi).
                        # v9.0-A HESAP BASLA (kabul testleri bu blogu marker'la calistirir)
                        _sw9 = durum.swing_seviyeler
                        _now9 = time.time()
                        if _sw9:
                            _g9 = [s for s in _sw9 if not s.get('gizli')]
                            _hs_say = len(_g9)
                            _yaslar9 = sorted((_now9 - s['ilk_gorulme_ts']) / 60.0
                                              for s in _g9 if s.get('ilk_gorulme_ts'))
                            _hm_yas = (round(_yaslar9[len(_yaslar9) // 2], 1)
                                       if _yaslar9 else None)
                            # grab filtresinin BIREBIR tersi (satir ~5129; sabit ayni sembol)
                            _hg_uygun = sum(1 for s in _sw9
                                            if not (s.get('gizli')
                                                    or (s.get('guc') or 0) < SWING_SEVIYE_MIN_GUC))
                        else:
                            _hs_say = None
                            _hm_yas = None
                            _hg_uygun = None
                        # v9.0-A HESAP BITIR
                        supabase.table("balina_avcisi_data").update({
                            "seviye_guc": _ys8.get('guc'),
                            "seviye_yasi_dk": (round((time.time() - _igt8) / 60.0, 1)
                                               if _igt8 else None),
                            "seviye_yenileme_sayisi": _ys8.get('yenileme_sayisi'),
                            "delme_min_belirleyen": _td8.get('delme_min_belirleyen'),
                            "delme_atr_kati": _td8.get('delme_atr_kati'),
                            "lik_yog_yon": _td8.get('lik_yog_yon'),
                            "lik_yog_ters": _td8.get('lik_yog_ters'),
                            "lik_iki_tarafli": _td8.get('lik_iki_tarafli'),
                            "mum_ici_konum": _td8.get('mum_ici_konum'),
                            "lik_pencere_damgasi": durum.lik_pencere_damgasi,
                            "lik_borsa_sayisi": durum.lik_borsa_sayisi,
                            "lik_donma_sayaci": durum.lik_donma_sayaci,
                            "harita_seviye_sayisi": _hs_say,    # v9.0-A: SALT KAYIT
                            "harita_medyan_yas_dk": _hm_yas,
                            "harita_grab_uygun": _hg_uygun,
                        }).eq("id", yeni_satir_id).execute()
                    except Exception as e:
                        logging.warning(f"teshis kolonlari UPDATE hatasi (kolonlar ALTER edildi mi?): {e}")

                # ---- v9.3 GOLGE: AYRI best-effort UPDATE. BILINCLI SPEC SAPMASI:
                # spec C "ayni try icinde" diyordu; ama PostgREST bilinmeyen TEK
                # kolonda TUM update'i reddeder — ayni cagriya koymak, ALTER
                # kosulana kadar 15 mevcut teshis kolonunu da bosaltirdi (v8.8-F
                # deployunda birebir yasandi). Ayri cagri: golge kolonlari henuz
                # yoksa YALNIZ golge kaybolur, teshis akisi surer. Golge cogu
                # dakika None — yalniz golge VARKEN yazilir (NULL zaten varsayilan;
                # bos dakikada ek DB cagrisi israf, v9.2 kadans ilkesiyle ayni). ----
                if SWING_ARSIV_AKTIF and yeni_satir_id and golge_yon is not None:
                    try:
                        supabase.table("balina_avcisi_data").update({
                            "golge_yon": golge_yon,      # v9.3 GOLGE: SALT KAYIT —
                            "golge_kapi": golge_kapi,    # reddedilmis ikiz; islem
                            "golge_skor": golge_skor,    # cagrisi DEGIL (spec F)
                        }).eq("id", yeni_satir_id).execute()
                        logging.info(f"GOLGE SINYAL: {golge_yon} skor={golge_skor} "
                                     f"kapi={golge_kapi} (kayit; islem DEGIL)")
                    except Exception as e:
                        logging.warning(f"golge kolonlari UPDATE hatasi (ALTER edildi mi?): {e}")

                if kalite['cvd_guvenilir'] and (tasfiye_a or supurme_yeni_onaylar):
                    olaylar = []
                    ham = {
                        "d_vadeli": round(pencere['d_vadeli_cvd'], 1) if pencere else None,
                        "d_spot": round(pencere['d_spot_cvd'], 1) if pencere else None,
                        "d_fiyat_pct": round(pencere['d_fiyat_pct'], 4) if pencere else None,
                        "d_oi_pct": round(d_oi_k, 4),
                        "esik_c_neg": round(esik_c_neg, 1),
                        "tasfiye_long_yogunluk": round(skor_girdi['tasfiye_long_yogunluk'], 2),
                        # ---- v7.5: EMİLİMİN YÖNÜ (surekli deger; yeni HUCRE ACILMAZ) ----
                        # 2x2 kohort yapisi KORUNUR. Ucuncu faktoru hucre yapmak
                        # 8 hucre demekti; ~30 olayda hucre basi n~4 -> hicbir sey
                        # soylenemez ama biri sansen parlar ve "kesif" sanilir.
                        # Surekli deger yazilir; iki hafta sonra POST-HOC dilimlenir
                        # ve post-hoc OLDUGU BILINEREK yorumlanir.
                        # v7.9: v7.4'ten kalan MUKERRER blok kaldirildi — ayni 4 anahtar
                        # asagida emilim['...'] ile bir daha yaziliyordu (son atama
                        # kazandigi icin davranis ayniydi ama dogrudan indeksleme
                        # _bos_emilim'in anahtar setine kirilgan bagimlilikti).
                        "emilim_borsasi": (emilim or {}).get('emilim_borsasi'),
                        "spot_egilim": (emilim or {}).get('spot_egilim'),
                        "perp_egilim": (emilim or {}).get('perp_egilim'),
                        "emilim_esnekligi": (emilim or {}).get('emilim_esnekligi'),
                        "satici_tukenmesi": (emilim or {}).get('satici_tukenmesi'),
                        "sonme_orani": (emilim or {}).get('sonme_orani'),
                        "alici_tukenmesi": (emilim or {}).get('alici_tukenmesi'),      # v7.6
                        "alici_sonme_orani": (emilim or {}).get('alici_sonme_orani'),
                        "spot_borsa_sayisi": (emilim or {}).get('spot_borsa_sayisi'),  # v7.6 mutabakat
                        "spot_bid_agir_sayi": (emilim or {}).get('spot_bid_agir_sayi'),
                        "spot_ask_agir_sayi": (emilim or {}).get('spot_ask_agir_sayi'),
                        "perp_borsa_sayisi": (emilim or {}).get('perp_borsa_sayisi'),  # v7.7 perp mutabakat
                        "perp_bid_agir_sayi": (emilim or {}).get('perp_bid_agir_sayi'),
                        "perp_ask_agir_sayi": (emilim or {}).get('perp_ask_agir_sayi'),
                        # v7.7: perp defterlerinin en yaslisi (sn). FAZ 2'de "perp
                        # ne siklikta bayatliyor, duvar vetosunu bayat oy kirletiyor mu?"
                        # sorusunu POST-HOC yanitlar; simdilik SADECE olculur.
                        "perp_ob_yasi_sn": round(perp_ob_yasi, 1) if perp_ob_yasi is not None else None,
                        # v7.8: bu satir HANGI cetvelle olculdu? (AGG=Coinalyze, WS=yedek)
                        # Faz-2 analizi WS-donemi satirlarini ayri tutabilsin diye.
                        "cvd_kaynak": cvd_kaynak_etiketi,
                        "spot_bid_d": round(spot_bid_d, 0) if spot_bid_d else None,
                        "spot_ask_d": round(spot_ask_d, 0) if spot_ask_d else None,
                        "tasfiye_short_yogunluk": round(skor_girdi['tasfiye_short_yogunluk'], 2),
                        "esik_lik_long_medyan": round(esik_lik_long_med, 0),
                        "esik_lik_short_medyan": round(esik_lik_short_med, 0),
                        "esik_volatilite": round(esik_vol, 4),
                        "ws_lik_long_5dk": round(ws_lik_long, 0),
                        "ws_lik_short_5dk": round(ws_lik_short, 0),
                        "aktif_borsa": aktif_borsa_sayisi,
                        "funding": funding_rate,
                        "spot_cvd_giris": round(spot_cvd, 1),
                        "oi_giris": round(open_interest, 0),
                        "tasfiye_yonu": tasfiye_yonu,   # v7.3.1: hangi taraf flush oldu
                        # v7.9: eski v7.4 blogundan TEKIL kalan iki anahtar guvenli
                        # (.get) bicimde tasindi; mukerrer 4'lusu yukarida tek kez var.
                        "emilim_spot_pay": (emilim or {}).get('emilim_spot_pay'),
                        "esik_spot_neg": (emilim or {}).get('esik_spot_neg'),
                    }
                    if supurme_yeni_onaylar:
                        for yon_o, o in supurme_yeni_onaylar:
                            stop_ref = o['fitil_uc']
                            olaylar.append({
                                "id": yeni_satir_id,
                                "zaman": datetime.datetime.utcnow().isoformat(),
                                "yon": yon_o, "rejim": rejim,
                                "tasfiye_var": bool(tasfiye_a),
                                "supurme_var": True,
                                "seviye": o['seviye'], "seviye_test_sayisi": o['test'],
                                "fitil_ucu": o['fitil_uc'],
                                "delme_vol_kati": round(o['delme_pct'] / esik_vol, 2) if esik_vol else None,
                                "giris_fiyati": anlik_fiyat, "stop_ref": stop_ref,
                                "ham": ham,
                                "spot_teyit_gecikmesi_dk": None,
                                "oi_erime_gecikmesi_dk": None,
                                "getiri": {}, "mae_mfe": {},
                            })
                    elif tasfiye_a:
                        # Yon hipotezi LONG: long-flush = satici tukenmesi/donus adayi,
                        # short-flush = squeeze yakiti. Hangi tarafin flush oldugu
                        # ham.tasfiye_yonu'nda — Faz 2 analizi oradan ayristirir.
                        yon_a = 'LONG'
                        simdi_cd2 = time.time()
                        # Kaskad boyunca her dakika yazma (Supabase sisirme); 10dk
                        # rising-edge — kumeleme ozeti zaten kume basindan okur.
                        if simdi_cd2 - durum.son_tasfiye_kohort_ts.get(yon_a, 0) >= 600:
                            durum.son_tasfiye_kohort_ts[yon_a] = simdi_cd2
                            vol_stop = (esik_vol or 0.02) * 2 / 100.0
                            stop_ref = anlik_fiyat * (1 - vol_stop) if yon_a == 'LONG' \
                                else anlik_fiyat * (1 + vol_stop)
                            olaylar.append({
                                "id": yeni_satir_id,
                                "zaman": datetime.datetime.utcnow().isoformat(),
                                "yon": yon_a, "rejim": rejim,
                                "tasfiye_var": True, "supurme_var": False,
                                "seviye": None, "seviye_test_sayisi": None,
                                "fitil_ucu": None, "delme_vol_kati": None,
                                "giris_fiyati": anlik_fiyat,
                                "stop_ref": round(stop_ref, 1),
                                "ham": ham,
                                "spot_teyit_gecikmesi_dk": None,
                                "oi_erime_gecikmesi_dk": None,
                                "getiri": {}, "mae_mfe": {},
                            })
                    if olaylar:
                        durum.kohort_bekleyen.extend(olaylar)
                        logging.info(f"TASFIYE KOHORTU -> {len(olaylar)} olay kuyrukta "
                                     f"(rejim={rejim}, supurme={bool(supurme_yeni_onaylar)})")

                # Tamponu bosalt (yeni olay olmasa da onceki turdan kalan olabilir);
                # yalnizca DOGRULANMIS yazimda temizle — olay kaybi yasak.
                if durum.kohort_bekleyen:
                    if _tasfiye_kohortuna_ekle(list(durum.kohort_bekleyen)):
                        logging.info(f"TASFIYE KOHORTU -> {len(durum.kohort_bekleyen)} "
                                     f"olay kalici olarak yazildi.")
                        durum.kohort_bekleyen = []
                    else:
                        logging.warning(f"Kohort yazimi basarisiz; {len(durum.kohort_bekleyen)} "
                                        f"olay tamponda, sonraki turda yeniden denenecek.")

                # v7.3 (spec §6 notu): TAZE_SATIS bonusu SILAHLI supurme fazinda kac
                # kez verildi? FAZ 2'nin ikinci sorusu — sayaci kohort meta'ya yaz.
                if rejim == 'TAZE_SATIS':
                    silahli_var = any(d.get('durum') in ('SILAHLI', 'DELINDI')
                                      for d in durum.supurme_dip_durumlari.values())
                    if silahli_var:
                        ozet_ve_analiz_dongusu._taze_satis_silahli = \
                            getattr(ozet_ve_analiz_dongusu, '_taze_satis_silahli', 0) + 1

                surec_log = ""
                if surec["surec_rejim"] != "NOTR":
                    surec_log = (f" || SÜREÇ: {surec['surec_rejim']} {surec['sure_dk']:.0f}dk "
                                 f"olgunluk={surec['olgunluk']:.2f} tukenme={surec['tukenme']}/4")
                    if surec["uyari"]:
                        surec_log += f" ⚠ {surec['uyari']}"
                logging.info(
                    f"VERI ENJEKTE (v4) -> Fiyat: ${anlik_fiyat:,.2f} | "
                    f"LongSkor: {long_skor} | ShortSkor: {short_skor} | Sinyal: {sinyal} | "
                    f"Rejim: {rejim} | {aciklama}{surec_log}"
                )
            else:
                logging.warning(f"Fiyat henuz 0 (son guncelleme {son_guncelleme_gecen:.0f}sn once).")

        except Exception as e:
            logging.error(f"Ozet/analiz hatasi: {e}")

        time.sleep(60)


# =========================================================================
# ==================  v2 YENİ: GERİ TEST (SELF-SCORING)  ==================
# =========================================================================
# Sistem artik kendi sinyalinin isabetini olcer. GERI_TEST_UFUK_DK dakika
# onceki kayitlarin skoru YON belirttiyse (long/short arasi belirgin fark),
# fiyat o yonde mi hareket etmis diye bakip is_win kolonunu doldurur.
# Boylece bir sinyalin curudugunu AYLAR sonra degil, saatler icinde gorursun.
# is_win = True  -> yonlu tahmin dogru cikti
# is_win = False -> yanlis cikti
# is_win = null  -> yon belirtilmemis (notr), degerlendirme disi
# =========================================================================
def geri_test_dongusu():
    """
    v6 — ÇOKLU UFUK ÖLÇÜM.
    Eski hali sadece 15dk'ya bakiyordu. Ama bir absorbsiyon/dagitim sinyali
    1-4 saatte olgunlasabilir; 15dk ona hakkini vermiyor olabilir.
    Artik AYNI ANDA 4 ufuk olculuyor: 15dk / 30dk / 60dk / 240dk.
      - is_win kolonu = 15dk sonucu (geriye uyumluluk, panel bunu okuyor)
      - Coklu ufuk istatistigi -> balina_ayarlar['geri_test_istatistik']
    Boylece iki hafta sonra "hangi ufukta kenar var" sorusuna KANITLA cevap veririz.
    Ayrica MALIYET CITASI uygulanir: getiri komisyonu (~%0.10) asmadiysa
    "kazanc" sayilmaz -- gercek kar olcusu.
    """
    time.sleep(120)
    LEAN_MARJI = 10.0        # long/short farki bu kadarsa "yonlu" sayilir
    UFUKLAR = [15, 30, 60, 240]   # dakika — MEVCUT (is_win + kohort BUNU kullanir, DEGISMEZ)
    UZUN_UFUKLAR_DK = [1440, 2880, 4320, 5760]   # v9.5: 1/2/3/4 gun (dakika)
    # NOT: UFUKLAR (kisa) ve UZUN_UFUKLAR_DK bilerek AYRI. is_win + tasfiye kohortu
    # yalniz UFUKLAR'a baglidir; birlestirme YOK (E2: _kohort_ileri_olc'a uzun ufuk
    # gecilirse olaylar kisa pencere disinda kalip KALICI 'olculemez' isaretlenir).
    # (Spec adi "v9.4 uzun ufuk" — v9.4 etiketi repoda kohort korumasinda
    # kullanildigi icin kod etiketi v9.5.)
    MALIYET_PCT = 0.10       # komisyon+spread+slippage tahmini (gidis-donus)

    while True:
        try:
            simdi = datetime.datetime.utcnow()
            # En uzun ufuk + pay kadar geriye bak
            pencere_bas = (simdi - datetime.timedelta(minutes=max(UFUKLAR) + 30)).isoformat()
            # v7.3: spot_cvd + open_interest EKLENDI (gecikmeli teyit olcumu icin;
            # sorgu zaten atiliyor — ek REST yok).
            res = (supabase.table("balina_avcisi_data")
                   .select("id,kayit_zamani,anlik_fiyat,long_skor,short_skor,"
                           "is_win,sinyal_durumu,spot_cvd,open_interest")
                   .gte("kayit_zamani", pencere_bas)
                   .order("kayit_zamani", desc=False)
                   .limit(5000)
                   .execute())
            satirlar = res.data or []
            if len(satirlar) < 20:
                time.sleep(180)
                continue

            zamanli = []
            for s in satirlar:
                try:
                    t = datetime.datetime.fromisoformat(
                        s['kayit_zamani'].replace('Z', '+00:00')).replace(tzinfo=None)
                    zamanli.append((t, s))
                except Exception:
                    continue
            zamanli.sort(key=lambda x: x[0])

            # ================= v9.5: UZUN UFUK — kadans + ayri sorgu =================
            # Kadans: uzun ufuk sonuclari dakikalar icinde degismez; 4 gunluk ~6000
            # satiri her 180sn'de cekmek israf. 10 turda bir (~30dk) hesapla.
            # (Desen: coinalyze_guncelle._fr_ls_tur, v9.2.)
            geri_test_dongusu._uzun_tur = getattr(geri_test_dongusu, '_uzun_tur', -1) + 1
            uzun_hesapla = (geri_test_dongusu._uzun_tur % 10 == 0)
            uzun_zamanli = []
            if uzun_hesapla:
                # 4 gun (5760dk) + 60dk pay geriye. Limit: 4 gun ~5760 dk-kaydi;
                # yeniden baslama/cakisma dakikada >1 kayit yazabilir -> limit(10000).
                uzun_pencere_bas = (simdi - datetime.timedelta(
                    minutes=max(UZUN_UFUKLAR_DK) + 60)).isoformat()
                try:
                    res_uzun = (supabase.table("balina_avcisi_data")
                        .select("id,kayit_zamani,anlik_fiyat,long_skor,short_skor,"
                                # v9.6: OB olcumu icin — duvar dilimi + golge dilimi
                                "order_book_depth_bid_1pct,order_book_depth_ask_1pct,"
                                "golge_yon,golge_kapi")
                        .gte("kayit_zamani", uzun_pencere_bas)
                        .order("kayit_zamani", desc=False)
                        .limit(10000)
                        .execute())
                    uzun_satirlar = res_uzun.data or []
                except Exception as e:
                    logging.warning(f"UZUN UFUK sorgu hatasi: {e}")
                    uzun_satirlar = []
                    uzun_hesapla = False
                # LIMIT ASIMI KORUMASI: 10000 dolduysa Supabase SESSIZCE ilk 10000'i
                # doner -> eksik pencere -> yanlis "kar" sonucu. Eksik > yanlis.
                if uzun_hesapla and len(uzun_satirlar) >= 10000:
                    logging.warning("UZUN UFUK: limit(10000) DOLU — pencere eksik "
                                    "olabilir, sayfalama gerekebilir. Bu tur ATLANIYOR.")
                    uzun_hesapla = False
                # Az-veri korumasi (kisa taraftaki 'len<20' esdegeri). Uzun ufuk zaten
                # az orneklem; 20 altinda hic olcme.
                if uzun_hesapla and len(uzun_satirlar) < 20:
                    uzun_hesapla = False
                if uzun_hesapla:
                    for s in uzun_satirlar:
                        try:
                            t = datetime.datetime.fromisoformat(
                                s['kayit_zamani'].replace('Z', '+00:00')).replace(tzinfo=None)
                            uzun_zamanli.append((t, s))
                        except Exception:
                            continue
                    uzun_zamanli.sort(key=lambda x: x[0])
            # bisect icin paralel zaman listesi (uzun_zamanli sirali — ayni sirada)
            uzun_ts = [t for (t, _s) in uzun_zamanli]
            # ======================================================================

            # Ufuk sonrasi fiyati bulan yardimci
            def sonraki_fiyat(t0, ufuk_dk):
                hedef = t0 + datetime.timedelta(minutes=ufuk_dk)
                for (t2, s2) in zamanli:
                    if t2 >= hedef:
                        f = float(s2.get('anlik_fiyat') or 0)
                        return f if f > 0 else None
                return None

            # v9.5-YARDIMCI BASLA (kabul testleri bu blogu marker'la calistirir)
            # v9.5: uzun ufuk icin AYRI ileri-fiyat — 'zamanli'ya DEGIL 'uzun_zamanli'ya
            # bakar. SPEC SAPMASI (belgeli): spec lineer tarama veriyordu; ~4300 olay x
            # ~5760 satir lineer tarama GIL'i onlarca saniye kilitleyip WS thread'lerini
            # ac birakirdi. bisect ile O(log n) — SEMANTIK BIREBIR AYNI: 'hedef'ten
            # buyuk-esit ILK satir alinir, fiyati gecersizse None (ileri taranmaz).
            # Esdegerlik kabul testinde lineer referansla kanitlanir.
            def _uzun_sonraki_fiyat(t0, ufuk_dk):
                hedef = t0 + datetime.timedelta(minutes=ufuk_dk)
                i = bisect.bisect_left(uzun_ts, hedef)
                if i >= len(uzun_zamanli):
                    return None
                f = float(uzun_zamanli[i][1].get('anlik_fiyat') or 0)
                return f if f > 0 else None

            # v9.5: trend yonu — SADECE t0'dan ONCEKI veri (look-ahead YOK).
            # t0 fiyati (f0) DISARIDAN gecilir; uzun_zamanli'da yeniden aranmaz.
            # Referans: (t0 - geri_saat) anindan ONCEKI son gecerli fiyat.
            # bisect: hedef'e kucuk-esit son indeks; gecersiz fiyatta geriye yuru
            # (lineer referansla ayni sonuc — 'son f>0' semantigi korunur).
            def _trend_yonu(t0, f0, geri_saat=6):
                hedef = t0 - datetime.timedelta(hours=geri_saat)
                i = bisect.bisect_right(uzun_ts, hedef) - 1
                ref_f = None
                while i >= 0:
                    f = float(uzun_zamanli[i][1].get('anlik_fiyat') or 0)
                    if f > 0:
                        ref_f = f
                        break
                    i -= 1
                if ref_f is None or f0 <= 0:
                    return None
                fark_pct = (f0 / ref_f - 1) * 100
                if fark_pct > 0.5:
                    return 'YUKARI'
                if fark_pct < -0.5:
                    return 'ASAGI'
                return None   # yatay (+-%0.5) — dilime sokma (belirsiz trend olculmez)
            # v9.5-YARDIMCI BITIR

            # ---- 1) is_win kolonu (15dk, geriye uyumluluk) ----
            guncellenen = 0
            for (t, s) in zamanli:
                if s.get('is_win') is not None:
                    continue
                if (simdi - t).total_seconds() / 60.0 < 15:
                    continue
                ls = float(s.get('long_skor') or 0)
                ss = float(s.get('short_skor') or 0)
                if abs(ls - ss) < LEAN_MARJI:
                    continue
                f0 = float(s.get('anlik_fiyat') or 0)
                if f0 <= 0:
                    continue
                f1 = sonraki_fiyat(t, 15)
                if not f1:
                    continue
                lean_long = ls > ss
                dogru = (lean_long and f1 > f0) or ((not lean_long) and f1 < f0)
                try:
                    supabase.table("balina_avcisi_data").update(
                        {"is_win": bool(dogru)}).eq("id", s['id']).execute()
                    guncellenen += 1
                except Exception as e:
                    logging.warning(f"Geri test guncelleme hatasi (id={s.get('id')}): {e}")

            # ---- 2) ÇOKLU UFUK İSTATİSTİĞİ (asil olcum) ----
            # Her ufuk icin: yonlu kanaatlerin isabeti + maliyet-sonrasi getiri.
            # Ayrica SADECE GERCEK SINYALLER (LONG/SHORT) ayri olculur.
            istatistik = {"guncelleme": simdi.isoformat(), "ufuklar": {}}
            for ufuk in UFUKLAR:
                kanaat_dogru = 0; kanaat_top = 0
                kanaat_getiri = []
                sinyal_dogru = 0; sinyal_top = 0
                sinyal_getiri = []
                for (t, s) in zamanli:
                    if (simdi - t).total_seconds() / 60.0 < ufuk:
                        continue  # ufuk henuz dolmadi
                    ls = float(s.get('long_skor') or 0)
                    ss = float(s.get('short_skor') or 0)
                    f0 = float(s.get('anlik_fiyat') or 0)
                    if f0 <= 0:
                        continue
                    f1 = sonraki_fiyat(t, ufuk)
                    if not f1:
                        continue
                    sig = (s.get('sinyal_durumu') or '').upper()

                    # (a) YONLU KANAAT (skor farki >= LEAN_MARJI) — genis olcum
                    if abs(ls - ss) >= LEAN_MARJI:
                        lean = 1 if ls > ss else -1
                        getiri = (f1 / f0 - 1) * 100 * lean   # kanaat yonunde %
                        kanaat_top += 1
                        if getiri > 0:
                            kanaat_dogru += 1
                        kanaat_getiri.append(getiri)

                    # (b) GERCEK SINYAL (v5 kapilarindan gecmis) — dar olcum
                    if sig in ('LONG', 'SHORT'):
                        yon = 1 if sig == 'LONG' else -1
                        getiri = (f1 / f0 - 1) * 100 * yon
                        sinyal_top += 1
                        if getiri > 0:
                            sinyal_dogru += 1
                        sinyal_getiri.append(getiri)

                def ozet(dogru, top, getiriler):
                    if top == 0:
                        return {"n": 0}
                    ort = sum(getiriler) / len(getiriler)
                    # Maliyet sonrasi: her islem MALIYET_PCT oder
                    net = ort - MALIYET_PCT
                    return {
                        "n": top,
                        "isabet": round(100.0 * dogru / top, 1),
                        "ort_getiri": round(ort, 4),
                        "net_getiri": round(net, 4),   # maliyet dusulmus GERCEK kar
                        "karli_mi": bool(net > 0),
                    }

                istatistik["ufuklar"][f"{ufuk}dk"] = {
                    "kanaat": ozet(kanaat_dogru, kanaat_top, kanaat_getiri),
                    "sinyal": ozet(sinyal_dogru, sinyal_top, sinyal_getiri),
                }

            # ================= v9.5: UZUN UFUK + REJIM DILIMLI OLCUM =================
            # SALT OLCUM — sinyal davranisina, is_win'e, kisa 'ufuklar' ciktisina
            # dokunmaz. Rejim sorusu: kanaat trend yonunde miydi, karsisinda miydi?
            # (trend_yonunde karli + trend_karsisinda zararli ise sistem trendi
            # odunc aliyor demektir — Faz 2 kurali oradan dogar.)
            # v9.5-OLCUM BASLA (kabul testleri bu blogu marker'la calistirir)
            if uzun_hesapla and uzun_zamanli:
                def _uzun_ozet(dogru, top, getiriler):
                    if top == 0:
                        return {"n": 0}
                    ort = sum(getiriler) / len(getiriler)
                    net = ort - MALIYET_PCT
                    return {
                        "n": top,
                        "isabet": round(100.0 * dogru / top, 1),
                        "ort_getiri": round(ort, 4),
                        "net_getiri": round(net, 4),
                        "karli_mi": bool(net > 0),
                        # orneklem guveni — n<30 "isaret"tir, "kanit" DEGIL
                        "guvenilir": bool(top >= 30),
                    }
                uzun_ist = {}
                for ufuk in UZUN_UFUKLAR_DK:
                    kovalar = {
                        'tum':               {'d': 0, 'n': 0, 'g': []},
                        'trend_yonunde':     {'d': 0, 'n': 0, 'g': []},
                        'trend_karsisinda':  {'d': 0, 'n': 0, 'g': []},
                    }
                    for (t, s) in uzun_zamanli:
                        if (simdi - t).total_seconds() / 60.0 < ufuk:
                            continue   # ufuk henuz dolmadi
                        ls = float(s.get('long_skor') or 0)
                        ss = float(s.get('short_skor') or 0)
                        if abs(ls - ss) < LEAN_MARJI:
                            continue   # yonsuz — olcme
                        f0 = float(s.get('anlik_fiyat') or 0)
                        if f0 <= 0:
                            continue
                        f1 = _uzun_sonraki_fiyat(t, ufuk)
                        if not f1:
                            continue
                        lean = 1 if ls > ss else -1
                        getiri = (f1 / f0 - 1) * 100 * lean
                        # 'tum' kovasi (dilimlemesiz)
                        k = kovalar['tum']
                        k['n'] += 1
                        k['g'].append(getiri)
                        if getiri > 0:
                            k['d'] += 1
                        # rejim kovasi (trend belirliyse; yatay yalniz 'tum'da)
                        trend = _trend_yonu(t, f0)
                        if trend is not None:
                            kanaat_yon = 'YUKARI' if lean > 0 else 'ASAGI'
                            kova_ad = ('trend_yonunde' if kanaat_yon == trend
                                       else 'trend_karsisinda')
                            k = kovalar[kova_ad]
                            k['n'] += 1
                            k['g'].append(getiri)
                            if getiri > 0:
                                k['d'] += 1
                    uzun_ist[f"{ufuk // 1440}g"] = {
                        ad: _uzun_ozet(k['d'], k['n'], k['g'])
                        for ad, k in kovalar.items()
                    }
                istatistik["uzun_ufuklar"] = uzun_ist
            # KRITIK: _ayarlar_yaz TUM sozlugu yazar ve 'istatistik' her tur SIFIRDAN
            # kurulur — skip turunda 'uzun_ufuklar' sozluge geri konmazsa DB'deki son
            # uzun olcum SILINIR. Cozum: son olcumu cache'le, skip turunda geri koy
            # (v9.2 kadans ilkesi: skip turu son gercek olcumu korur).
            if uzun_hesapla and "uzun_ufuklar" in istatistik:
                geri_test_dongusu._son_uzun = istatistik["uzun_ufuklar"]
            elif hasattr(geri_test_dongusu, "_son_uzun"):
                istatistik["uzun_ufuklar"] = geri_test_dongusu._son_uzun
            # v9.5-OLCUM BITIR
            # ======================================================================

            # ================= v9.6: ORDER BOOK DEGER OLCUMU (SALT OLCUM) =================
            # Kullanici hipotezi: "60sn'lik REST snapshot'i copluk — order book'u
            # kaldiralim". Ev disiplini: KESMEDEN ONCE OLC. Iki dilim:
            #  (a) DUVAR UYUM: yonlu kanaat, o anki 1% derinlik dengesiyle ayni
            #      yondaysa 'duvar_lehte', tersse 'duvar_aleyhte'. Isabetler ESITSE
            #      order book kanaate bilgi KATMIYOR demektir (cop hipotezi lehine);
            #      lehte belirgin iyiyse duvar verisi is goruyor demektir.
            #  (b) GOLGE-DUVAR: 'duvar' kapisinin susturdugu golgeler ileride ne
            #      getirdi? (net>0 ise kapi HAKSIZ susturuyor -> gevsetme adayi;
            #      net<0 ise kapi dogru calisiyor.)
            # Karar yoluna, kapilara, skora DOKUNMAZ. v9.5 kadansini paylasir.
            # v9.6-OB BASLA (kabul testleri bu blogu marker'la calistirir)
            if uzun_hesapla and uzun_zamanli:
                OB_UFUKLAR_DK = [60, 240, 1440]   # v9.6: OB olcum ufuklari (dk)
                ob_ist = {}
                for ufuk in OB_UFUKLAR_DK:
                    kovalar_ob = {
                        'duvar_lehte':    {'d': 0, 'n': 0, 'g': []},
                        'duvar_aleyhte':  {'d': 0, 'n': 0, 'g': []},
                        'golge_duvar':    {'d': 0, 'n': 0, 'g': []},
                        'golge_diger':    {'d': 0, 'n': 0, 'g': []},
                    }
                    for (t, s) in uzun_zamanli:
                        if (simdi - t).total_seconds() / 60.0 < ufuk:
                            continue
                        f0 = float(s.get('anlik_fiyat') or 0)
                        if f0 <= 0:
                            continue
                        f1 = _uzun_sonraki_fiyat(t, ufuk)
                        if not f1:
                            continue
                        # (a) duvar uyum dilimi — yonlu kanaat + gecerli iki derinlik
                        ls = float(s.get('long_skor') or 0)
                        ss = float(s.get('short_skor') or 0)
                        if abs(ls - ss) >= LEAN_MARJI:
                            bid_d = float(s.get('order_book_depth_bid_1pct') or 0)
                            ask_d = float(s.get('order_book_depth_ask_1pct') or 0)
                            # sifir tuzagi: derinlik olculememis (0/None) ya da esitse
                            # dilime SOKMA — uydurma yon yok
                            if bid_d > 0 and ask_d > 0 and bid_d != ask_d:
                                lean = 1 if ls > ss else -1
                                duvar_yon = 1 if bid_d > ask_d else -1
                                kova_ad = ('duvar_lehte' if lean == duvar_yon
                                           else 'duvar_aleyhte')
                                getiri = (f1 / f0 - 1) * 100 * lean
                                k = kovalar_ob[kova_ad]
                                k['n'] += 1
                                k['g'].append(getiri)
                                if getiri > 0:
                                    k['d'] += 1
                        # (b) golge-duvar dilimi — golge yonunde ileri getiri
                        g_yon = s.get('golge_yon')
                        if g_yon in ('LONG', 'SHORT'):
                            yon = 1 if g_yon == 'LONG' else -1
                            getiri = (f1 / f0 - 1) * 100 * yon
                            kova_ad = ('golge_duvar'
                                       if 'duvar' in str(s.get('golge_kapi') or '')
                                       else 'golge_diger')
                            k = kovalar_ob[kova_ad]
                            k['n'] += 1
                            k['g'].append(getiri)
                            if getiri > 0:
                                k['d'] += 1
                    ob_ist[f"{ufuk}dk"] = {
                        ad: _uzun_ozet(k['d'], k['n'], k['g'])
                        for ad, k in kovalar_ob.items()
                    }
                istatistik["ob_olcum"] = ob_ist
            # kadans cache (v9.5 ile ayni ilke: skip turu son olcumu korur)
            if uzun_hesapla and "ob_olcum" in istatistik:
                geri_test_dongusu._son_ob = istatistik["ob_olcum"]
            elif hasattr(geri_test_dongusu, "_son_ob"):
                istatistik["ob_olcum"] = geri_test_dongusu._son_ob
            # v9.6-OB BITIR
            # ======================================================================

            # ---- 3) BV FİLTRE İSTATİSTİĞİ (veri kalitesi kaniti) ----
            with durum.lock:
                bv_ist = {
                    "toplam_tur": durum.bv_toplam_tur,
                    "dislanan_tur": durum.bv_dislanan_tur,
                    "dislanma_orani": round(
                        100.0 * durum.bv_dislanan_tur / durum.bv_toplam_tur, 1
                    ) if durum.bv_toplam_tur else 0.0,
                    "borsa_sayaclari": dict(durum.bv_dislanan_sayac),
                }
            istatistik["bv_filtre"] = bv_ist

            try:
                _ayarlar_yaz("geri_test_istatistik", istatistik)
            except Exception as e:
                logging.warning(f"Istatistik yazma hatasi: {e}")

            # ---- v7.3: TASFIYE KOHORTU ILERI OLCUMU (2x2 + MAE/stop) ----
            # v9.5 KILIT: kohort KISA ufuk sistemidir (kisa 'zamanli' penceresine
            # bagli). UZUN ufuk BURAYA GIRMEZ — UZUN_UFUKLAR_DK gecilirse olaylar
            # kisa pencere disinda kalip KALICI 'olculemez' isaretlenir ve
            # tasfiye_kohortu persist state'i bozulur (bkz. _kohort_ileri_olc
            # icindeki kesinti korumasi). Bu cagri DAIMA UFUKLAR (kisa) alir.
            kohort_ozet = _kohort_ileri_olc(zamanli, simdi, UFUKLAR, MALIYET_PCT)

            # Ozet log (her turda degil, 15 dakikada bir yeter)
            if not hasattr(geri_test_dongusu, "_son_log"):
                geri_test_dongusu._son_log = 0
            if time.time() - geri_test_dongusu._son_log > 900:
                geri_test_dongusu._son_log = time.time()
                parcalar = []
                for ufuk in UFUKLAR:
                    k = istatistik["ufuklar"][f"{ufuk}dk"]["kanaat"]
                    if k.get("n", 0) > 10:
                        parcalar.append(
                            f"{ufuk}dk: %{k['isabet']} (n={k['n']}, net {k['net_getiri']:+.3f}%)"
                        )
                if parcalar:
                    logging.info("GERI TEST COKLU UFUK -> " + " | ".join(parcalar))
                # v9.5: uzun ufuk kanaat ozeti (varsa; salt gozlem)
                uzun_st = istatistik.get("uzun_ufuklar") or {}
                parcalar_u = []
                for ad, kv in uzun_st.items():
                    tum = kv.get('tum') or {}
                    if tum.get('n', 0) > 3:
                        g = "OK" if tum.get('guvenilir') else "az-n"
                        parcalar_u.append(
                            f"{ad}: %{tum.get('isabet')} (n={tum['n']},{g}, "
                            f"net {tum.get('net_getiri'):+.3f}%)")
                if parcalar_u:
                    logging.info("GERI TEST UZUN UFUK -> " + " | ".join(parcalar_u))
                # v9.6: OB deger olcumu ozeti (duvar bilgi katiyor mu? kapi hakli mi?)
                ob_st = (istatistik.get("ob_olcum") or {}).get("240dk") or {}
                _le, _al = ob_st.get('duvar_lehte') or {}, ob_st.get('duvar_aleyhte') or {}
                _gd = ob_st.get('golge_duvar') or {}
                if _le.get('n', 0) > 3 and _al.get('n', 0) > 3:
                    logging.info(
                        f"OB OLCUM 240dk -> duvar_lehte %{_le.get('isabet')} (n={_le['n']}) "
                        f"vs aleyhte %{_al.get('isabet')} (n={_al['n']})"
                        + (f" | golge_duvar net {_gd.get('net_getiri'):+.3f}% (n={_gd['n']})"
                           if _gd.get('n', 0) > 3 else ""))
                if bv_ist["toplam_tur"] > 0:
                    logging.info(
                        f"BV-FILTRE ISTATISTIK -> {bv_ist['dislanan_tur']}/{bv_ist['toplam_tur']} "
                        f"turda borsa dislandi (%{bv_ist['dislanma_orani']}) | "
                        f"suclular: {bv_ist['borsa_sayaclari'] or 'yok'}"
                    )
                # v7.3: kohort 2x2 ozeti (15dk'da bir)
                if kohort_ozet:
                    parcalar2 = []
                    for hucre, h in kohort_ozet.items():
                        u = h.get('15dk')
                        if isinstance(u, dict) and u.get('n'):
                            parcalar2.append(
                                f"{hucre}: n={h['n']} 15dk %{u['isabet']} "
                                f"net {u['net_getiri']:+.3f}% stop %{u['stop_orani']}")
                    if parcalar2:
                        logging.info("TASFIYE KOHORTU 2x2 -> " + " | ".join(parcalar2))

            if guncellenen > 0:
                logging.info(f"GERI TEST: {guncellenen} kaydin is_win degeri islendi.")
        except Exception as e:
            logging.warning(f"Geri test dongusu hatasi: {e}")
        time.sleep(180)


# =========================================================================
# ANA GİRİŞ
# =========================================================================
if __name__ == "__main__":
    threading.Thread(target=web_sunucu_calistir, daemon=True).start()
    threading.Thread(target=rest_yardimci_guncelle, daemon=True).start()
    threading.Thread(target=coinalyze_guncelle, daemon=True).start()
    threading.Thread(target=websocket_calistir, daemon=True).start()
    threading.Thread(target=likidasyon_websocket_calistir, daemon=True).start()
    threading.Thread(target=spot_websocket_calistir, daemon=True).start()
    threading.Thread(target=adaptif_esik_guncelle, daemon=True).start()
    threading.Thread(target=geri_test_dongusu, daemon=True).start()  # v2 YENİ

    ozet_ve_analiz_dongusu()
