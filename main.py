import json
import time
import os
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
        self.agg_spot_cvd = 0.0
        self.agg_vadeli_cvd = 0.0
        self.coinalyze_saglikli = False
        self.coinalyze_cvd_saglikli = False

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

        self.son_guncelleme = time.time()

durum = CanliDurum()

BUYUK_EMIR_ESIGI_USDT = 500_000.0
EMIR_OLGUNLUK_SANIYE = 300
ESIK_GUNCELLEME_ARALIGI = 600
MIN_KAYIT_ADAPTIF = 100

# v2 YENİ: skorlama parametreleri
PENCERE_DK = 5            # değişim kaç dakikalık pencerede ölçülsün
# ================== v5 — BALİNA DİSİPLİNİ ==================
# Veri kanıtı (1753 kayıt, 29 saat): skor 65-85 arası sinyaller yazı-tura
# (%47-53), skor 95+ sinyaller %71 isabetli. Sonuç: sistem çok konuşuyordu.
# Balina gibi: acele yok, her harekete tepki yok; tüm koşullar hizalanmadan
# tek kelime yok. Nadir ama nokta atışı.
SINYAL_ESIGI = 90.0       # 65 -> 90: sadece en güçlü kurulumlar konuşur
SINYAL_MARJI = 25.0       # kazanan taraf ezici üstün olmalı (flip-flop imkansız)
SINYAL_COOLDOWN_SN = 1800 # bir sinyalden sonra 30dk sus (ayni hareketi 6 kez sinyalleme)
MALIYET_CITASI_PCT = 0.30 # kurulum en az %0.30 hareket vaat etmeli (maliyet ~%0.10'un 3 kati)
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
      /mobil       -> telefon paneli (balina_mobil.html)
      /panel       -> masaustu paneli (v3balina_sonar_terminal.html)
    Panel dosyalari main.py ile AYNI KLASORDE olmali (repo koku).
    Boylece GitHub Pages / ayri repo / dosya transferi gerekmez;
    tek link: https://<servis>.onrender.com/mobil
    """

    PANEL_DOSYALARI = {
        "/mobil": "balina_mobil.html",
        "/panel": "v3balina_sonar_terminal.html",
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
            b"<a href='/mobil'>Mobil Panel</a>"
            b"<a href='/panel'>Masaustu Panel</a>"
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
            except Exception as e:
                logging.warning(f"Bybit derinlik hatasi: {e}")

            time.sleep(0.3)

            # D: OKX order book (3. borsa derinligi — spoofing tespitini guclendirir)
            # ÖNEMLİ: BTC-USDT-SWAP'ta 1 kontrat = 0.0001 BTC (100 kontrat = 0.01 BTC).
            # Kontrat degeri (ctVal) borsanin instruments API'sinden BİR KEZ cekilir;
            # OKX ileride degistirse kod kendini duzeltir. Cekilemezse guvenli
            # varsayilan 0.0001 kullanilir. (Eski 0.01 sabiti 100x YANLISTI.)
            try:
                if not hasattr(rest_yardimci_guncelle, "_okx_ctval"):
                    rest_yardimci_guncelle._okx_ctval = 0.0001  # guvenli varsayilan
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


def _ayarlar_yaz(anahtar, deger):
    try:
        supabase.table("balina_ayarlar").upsert({
            "anahtar": anahtar,
            "deger": deger,
            "guncellenme_zamani": datetime.datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        logging.warning(f"Ayarlar yazma hatasi ({anahtar}): {e}")


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
            if liq_res.status_code == 200:
                data = liq_res.json()
                if isinstance(data, list):
                    for borsa in data:
                        for nokta in borsa.get('history', []):
                            long_liq += float(nokta.get('l', 0) or 0)
                            short_liq += float(nokta.get('s', 0) or 0)
                    durum.coinalyze_saglikli = True
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

            fr_url = f"{COINALYZE_BASE}/funding-rate?symbols={sembol_param}"
            fr_res = session.get(fr_url, headers=headers, timeout=15)
            agg_fr = 0.0
            if fr_res.status_code == 200:
                data = fr_res.json()
                if isinstance(data, list) and len(data) > 0:
                    degerler = [float(b.get('value', 0) or 0) for b in data]
                    agg_fr = sum(degerler) / len(degerler)

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
                durum.agg_open_interest = agg_oi
                durum.agg_funding = agg_fr
                durum.agg_ls_ratio = agg_ls
                if vadeli_cvd_hesaplandi:
                    durum.agg_vadeli_cvd = yeni_vadeli_cvd
                    durum.coinalyze_cvd_saglikli = True
                if spot_cvd_hesaplandi:
                    durum.agg_spot_cvd = yeni_spot_cvd
                agg_vadeli_cvd_log = durum.agg_vadeli_cvd
                agg_spot_cvd_log = durum.agg_spot_cvd

            logging.info(
                f"COINALYZE AGG ({len(semboller)}v/{len(spot_semboller)}s borsa) -> "
                f"LongLiq: ${long_liq:,.0f} | ShortLiq: ${short_liq:,.0f} | "
                f"OI: ${agg_oi:,.0f} | Funding: {agg_fr:.5f} | L/S: {agg_ls:.2f} | "
                f"VadeliCVD: {agg_vadeli_cvd_log:,.0f}{'(yeni)' if vadeli_cvd_hesaplandi else '(korunan)'} | "
                f"SpotCVD: {agg_spot_cvd_log:,.0f}{'(yeni)' if spot_cvd_hesaplandi else '(korunan)'}"
            )

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

        elif 'bookTicker' in stream:
            best_bid = float(payload.get('b', 0))
            best_ask = float(payload.get('a', 0))
            best_bid_qty = float(payload.get('B', 0))
            best_ask_qty = float(payload.get('A', 0))
            if best_bid > 0 and best_ask > 0:
                orta_fiyat = (best_bid + best_ask) / 2
                with durum.lock:
                    durum.anlik_fiyat = orta_fiyat
                    if durum.son_tick_fiyat > 0:
                        if orta_fiyat > durum.son_tick_fiyat:
                            signed = best_ask_qty
                        elif orta_fiyat < durum.son_tick_fiyat:
                            signed = -best_bid_qty
                        else:
                            signed = 0
                        if signed != 0:
                            durum.trade_gecmisi.append((simdi_ms, signed))
                            sinir = simdi_ms - 15 * 60 * 1000
                            while durum.trade_gecmisi and durum.trade_gecmisi[0][0] < sinir:
                                durum.trade_gecmisi.popleft()
                    durum.son_tick_fiyat = orta_fiyat

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
        simdi_ms = int(time.time() * 1000)
        with durum.lock:
            durum.likidasyonlar.append((simdi_ms, usdt))
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


def adaptif_esik_guncelle():
    time.sleep(30)
    while True:
        try:
            simdi = time.time()
            yedi_gun_once = datetime.datetime.utcnow() - datetime.timedelta(days=7)
            bir_gun_once = datetime.datetime.utcnow() - datetime.timedelta(days=1)

            res = (supabase.table("balina_avcisi_data")
                   .select("kayit_zamani,order_book_depth_bid_1pct,liquidation_pool_volume,vadeli_cvd")
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
            uzun_cvd = []

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
                if c is not None:
                    uzun_cvd.append(float(c))

            kisa_derinlik = _aykiri_degerleri_temizle(kisa_derinlik)
            uzun_derinlik = _aykiri_degerleri_temizle(uzun_derinlik)
            kisa_likid = _aykiri_degerleri_temizle(kisa_likid)
            uzun_likid = _aykiri_degerleri_temizle(uzun_likid)
            uzun_cvd = _aykiri_degerleri_temizle(uzun_cvd)

            d_kisa = _yuzdelik(kisa_derinlik, 0.90)
            d_uzun = _yuzdelik(uzun_derinlik, 0.90)
            yeni_derinlik = max([x for x in [d_kisa, d_uzun] if x is not None], default=None)

            kisa_likid_nz = [x for x in kisa_likid if x > 0]
            uzun_likid_nz = [x for x in uzun_likid if x > 0]
            l_kisa = _yuzdelik(kisa_likid_nz, 0.75)
            l_uzun = _yuzdelik(uzun_likid_nz, 0.75)
            yeni_likid = max([x for x in [l_kisa, l_uzun] if x is not None], default=None)

            yeni_cvd_neg = _yuzdelik(uzun_cvd, 0.10)
            yeni_cvd_poz = _yuzdelik(uzun_cvd, 0.90)

            with durum.lock:
                if yeni_derinlik and yeni_derinlik > 0:
                    durum.esik_derinlik = yeni_derinlik
                if yeni_likid and yeni_likid > 0:
                    durum.esik_likidasyon = yeni_likid
                if yeni_cvd_neg is not None and yeni_cvd_neg < 0:
                    durum.esik_cvd_negatif = yeni_cvd_neg
                if yeni_cvd_poz is not None and yeni_cvd_poz > 0:
                    durum.esik_cvd_pozitif = yeni_cvd_poz
                durum.esik_guncelleme_zamani = simdi
                durum.esik_veri_sayisi = len(veriler)

            logging.info(
                f"ADAPTIF ESIK GUNCELLENDI ({len(veriler)} kayit) -> "
                f"Derinlik: ${durum.esik_derinlik:,.0f} | Likid: ${durum.esik_likidasyon:,.0f} | "
                f"CVD-satis: {durum.esik_cvd_negatif:,.0f} | CVD-alis: {durum.esik_cvd_pozitif:,.0f}"
            )
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
    yonlu = rejim in ("TEPE_DAGITIM", "DIP_TOPLAMA", "SHORT_SQUEEZE", "TAZE_ALIM",
                      "TAZE_SATIS", "LONG_TASFIYE")

    # Surec devami mi, yeni surec mi?
    ayni_aile = {
        "TEPE_DAGITIM": {"TEPE_DAGITIM", "SHORT_SQUEEZE"},   # dagitim + squeeze = ayni hikaye
        "SHORT_SQUEEZE": {"TEPE_DAGITIM", "SHORT_SQUEEZE"},
        "DIP_TOPLAMA": {"DIP_TOPLAMA", "LONG_TASFIYE"},
        "LONG_TASFIYE": {"DIP_TOPLAMA", "LONG_TASFIYE"},
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
    Donus: (long_skor, short_skor, sinyal, rejim, aciklama)
    """
    # ---- A) VERİ KALİTE KAPISI: kotu veriyle ASLA skor uretme ----
    if not kalite['cvd_guvenilir']:
        return 0.0, 0.0, "BEKLE", "VERI_GUVENSIZ", f"Kalite reddi: {kalite['sebep']}"
    if pencere is None:
        return 0.0, 0.0, "BEKLE", "VERI_BEKLENIYOR", "Pencere dolmadi"

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
    if ceyreklik_expiry_yakin_mi(datetime.datetime.now(), esik_saat=48):
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
    VETO_TAVANI = SINYAL_ESIGI - 15.0  # 75: veto rejiminde sinyal matematiksel imkansiz
    rejim = "NOTR"
    if d_fiyat > 0.02 and d_oi > 0.05:
        rejim = "TAZE_ALIM"; long_skor *= 1.20      # saglikli -> odul
    elif d_fiyat > 0.02 and d_oi < -0.05:
        rejim = "SHORT_SQUEEZE"
        long_skor = min(long_skor, VETO_TAVANI)     # VETO
    elif d_fiyat < -0.02 and d_oi > 0.05:
        rejim = "TAZE_SATIS"; short_skor *= 1.20
    elif d_fiyat < -0.02 and d_oi < -0.05:
        rejim = "LONG_TASFIYE"
        short_skor = min(short_skor, VETO_TAVANI)   # VETO

    long_skor = max(0.0, min(100.0, long_skor))
    short_skor = max(0.0, min(100.0, short_skor))

    if rejim == "NOTR":
        if absorbsiyon_long > 0.45:
            rejim = "DIP_TOPLAMA"
        elif absorbsiyon_short > 0.45:
            rejim = "TEPE_DAGITIM"

    # =================== v5 SİNYAL — BALİNA DİSİPLİNİ ===================
    # Balina her harekete tepki vermez. Sinyal = TÜM koşulların KESİŞİMİ.
    # Ortalama/telafi yok: tek bir katman zayıfsa sinyal YOK.
    sinyal = "BEKLE"
    ve_kapisi_log = ""

    # -- VE-KAPISI 1: her kritik katman kendi minimumunu GEÇMELİ --
    long_kapilar = {
        "islem": satis_yogunlugu >= VE_ISLEM_MIN,
        "direnc": fiyat_direnci_long >= VE_DIRENC_MIN,
        "duvar": duvar_teyitli_long >= VE_DUVAR_MIN,
    }
    short_kapilar = {
        "islem": alis_yogunlugu >= VE_ISLEM_MIN,
        "direnc": fiyat_zayifligi_short >= VE_DIRENC_MIN,
        "duvar": duvar_teyitli_short >= VE_DUVAR_MIN,
    }
    long_ve = all(long_kapilar.values())
    short_ve = all(short_kapilar.values())

    # -- VE-KAPISI 2: süreç bağlamı (trende karşı sinyal YASAK) --
    # surec: a['surec_rejim'] — dagitim surerken LONG verilmez, toplama
    # surerken SHORT verilmez. Surec olgunlasip tukenme 3+ olursa ters
    # yon serbest kalir (donus artik gercekci).
    surec_rejim = a.get('surec_rejim', 'NOTR')
    surec_tukenme = a.get('surec_tukenme', 0)
    dagitim_ailesi = surec_rejim in ('TEPE_DAGITIM', 'SHORT_SQUEEZE', 'TAZE_SATIS')
    toplama_ailesi = surec_rejim in ('DIP_TOPLAMA', 'TAZE_ALIM')
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

    # -- NİHAİ KARAR: skor + marj + VE-kapıları + hedef --
    if (long_skor >= SINYAL_ESIGI and (long_skor - short_skor) >= SINYAL_MARJI
            and long_ve and hedef_var_long):
        sinyal = "LONG"
    elif (short_skor >= SINYAL_ESIGI and (short_skor - long_skor) >= SINYAL_MARJI
            and short_ve and hedef_var_short):
        sinyal = "SHORT"

    # Log için hangi kapının kapalı olduğunu kaydet (öğrenmek için)
    if sinyal == "BEKLE" and max(long_skor, short_skor) >= SINYAL_ESIGI:
        taraf = long_kapilar if long_skor > short_skor else short_kapilar
        kapali = [k for k, v in taraf.items() if not v]
        if long_skor > short_skor and not hedef_var_long:
            kapali.append("hedef")
        if short_skor > long_skor and not hedef_var_short:
            kapali.append("hedef")
        if (long_skor > short_skor and dagitim_ailesi and surec_tukenme < 3):
            kapali.append("surec")
        if (short_skor > long_skor and toplama_ailesi and surec_tukenme < 3):
            kapali.append("surec")
        ve_kapisi_log = f" VE-RED:{','.join(kapali) if kapali else 'marj'}"

    duvar_durum = "teyitli" if (duvar_teyitli_long > 0 or duvar_teyitli_short > 0) else "teyitsiz"
    aciklama = (f"absL={absorbsiyon_long:.2f} absS={absorbsiyon_short:.2f} "
                f"dFiyat={d_fiyat:+.3f}% dOI={d_oi:+.2f}% "
                f"dVadeliCVD={d_vadeli:+,.0f} dSpotCVD={d_spot:+,.0f} "
                f"iraksama={iraksama:+.2f} borsa={aktif_borsa} "
                f"duvar={duvar_durum} rejim={rejim}{ve_kapisi_log}")

    return long_skor, short_skor, sinyal, rejim, aciklama


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
                cvd_kaynak_saglikli = durum.coinalyze_cvd_saglikli  # A: kalite girdisi
                if durum.coinalyze_cvd_saglikli:
                    calculated_cvd = durum.agg_vadeli_cvd
                    spot_cvd = durum.agg_spot_cvd
                else:
                    calculated_cvd = ws_vadeli_cvd
                    spot_cvd = ws_spot_cvd

                # ---- %1 DERİNLİK: Binance, Bybit ve OKX AYRI hesaplaniyor ----
                bnb_bid_d = 0.0; bnb_ask_d = 0.0
                byb_bid_d = 0.0; byb_ask_d = 0.0
                okx_bid_d = 0.0; okx_ask_d = 0.0
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
                    alt_limit = anlik_fiyat * 0.99
                    ust_limit = anlik_fiyat * 1.01
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
                coinalyze_ok = durum.coinalyze_saglikli

                esik_d = durum.esik_derinlik
                esik_l = durum.esik_likidasyon
                esik_c_neg = durum.esik_cvd_negatif
                esik_c_poz = durum.esik_cvd_pozitif

                son_guncelleme_gecen = time.time() - durum.son_guncelleme

            if agg_oi > 0:
                open_interest = agg_oi
            if agg_funding != 0:
                funding_rate = agg_funding

            # ================= v2: ROLLING SERİYE EKLE + PENCERE DEĞİŞİMİ =================
            simdi_epoch = time.time()
            anlik_kayit = {
                'ts': simdi_epoch, 'fiyat': anlik_fiyat,
                'bid_d': order_book_depth_bid_1pct, 'ask_d': order_book_depth_ask_1pct,
                'bnb_delta': bnb_delta, 'byb_delta': byb_delta, 'okx_delta': okx_delta,
                'vadeli_cvd': calculated_cvd, 'spot_cvd': spot_cvd, 'oi': open_interest
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
                # En az ~3 dk gecmis olsun ki degisim anlamli olsun
                if (simdi_epoch - eski['ts']) >= 150 and eski['fiyat'] > 0:
                    d_vadeli = calculated_cvd - eski['vadeli_cvd']
                    d_spot = spot_cvd - eski['spot_cvd']
                    pencere = {
                        'd_fiyat_pct': (anlik_fiyat / eski['fiyat'] - 1.0) * 100.0,
                        'd_vadeli_cvd': d_vadeli,
                        'd_spot_cvd': d_spot,
                        'd_oi_pct': (open_interest / eski['oi'] - 1.0) * 100.0 if eski['oi'] > 0 else 0.0,
                        # C: CVD IRAKSAMA — spot ve vadeli AYNI yone mi bakiyor?
                        # Ayni yon = teyit (guclu). Zit yon = iraksama (kaldiracli/kirilgan).
                        'cvd_iraksama': _cvd_iraksama_hesapla(d_vadeli, d_spot),
                    }

            # ================= v2: BAĞLAMSAL SKORU HESAPLA =================
            # v5.2: en yakin CIDDI duvar — artik UC BORSA birlesik kovalardan.
            # Sadece MUTABAKATLI (2+ borsa) VEYA cok buyuk tek-borsa duvarlari sayilir;
            # boylece tek borsadaki spoof duvar hedef sanilmaz.
            def _ciddi_duvarlar(kovalar):
                out = []
                for kf, d in kovalar.items():
                    mutabakat = len(d['borsalar'])
                    if d['usdt'] >= BUYUK_EMIR_ESIGI_USDT and (mutabakat >= 2 or d['usdt'] >= BUYUK_EMIR_ESIGI_USDT * 3):
                        out.append(kf)
                return out
            ciddi_ask = _ciddi_duvarlar(ask_kovalar)
            ciddi_bid = _ciddi_duvarlar(bid_kovalar)
            en_yakin_ask_fiyat = min((f for f in ciddi_ask if f > anlik_fiyat), default=0)
            en_yakin_bid_fiyat = max((f for f in ciddi_bid if f < anlik_fiyat), default=0)

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
            }
            # A) VERİ KALİTE KAPISI — kotu veriyle skor uretme
            kalite = veri_kalitesi_degerlendir(
                cvd_kaynak_saglikli=cvd_kaynak_saglikli,
                open_interest=open_interest,
                anlik_fiyat=anlik_fiyat,
                son_guncelleme_gecen=son_guncelleme_gecen,
                funding=funding_rate
            )
            long_skor, short_skor, sinyal, rejim, aciklama = balina_skoru_hesapla(
                skor_girdi, pencere, kalite)

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
                "sinyal_durumu": sinyal,
                "yakin_likidite_bid": likidite_bid_json,
                "yakin_likidite_ask": likidite_ask_json
            }

            if anlik_fiyat > 0:
                supabase.table("balina_avcisi_data").insert(payload).execute()
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
    UFUKLAR = [15, 30, 60, 240]   # dakika
    MALIYET_PCT = 0.10       # komisyon+spread+slippage tahmini (gidis-donus)

    while True:
        try:
            simdi = datetime.datetime.utcnow()
            # En uzun ufuk + pay kadar geriye bak
            pencere_bas = (simdi - datetime.timedelta(minutes=max(UFUKLAR) + 30)).isoformat()
            res = (supabase.table("balina_avcisi_data")
                   .select("id,kayit_zamani,anlik_fiyat,long_skor,short_skor,is_win,sinyal_durumu")
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

            # Ufuk sonrasi fiyati bulan yardimci
            def sonraki_fiyat(t0, ufuk_dk):
                hedef = t0 + datetime.timedelta(minutes=ufuk_dk)
                for (t2, s2) in zamanli:
                    if t2 >= hedef:
                        f = float(s2.get('anlik_fiyat') or 0)
                        return f if f > 0 else None
                return None

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
                if bv_ist["toplam_tur"] > 0:
                    logging.info(
                        f"BV-FILTRE ISTATISTIK -> {bv_ist['dislanan_tur']}/{bv_ist['toplam_tur']} "
                        f"turda borsa dislandi (%{bv_ist['dislanma_orani']}) | "
                        f"suclular: {bv_ist['borsa_sayaclari'] or 'yok'}"
                    )

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
