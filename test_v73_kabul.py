"""
v7.3 KABUL TESTLERİ
1) EŞDEĞERLİK KANITI (kabul #2): 500 rastgele girdi — v7.2 (git HEAD) ve v7.3
   balina_skoru_hesapla BİREBİR aynı (long, short, sinyal) döndürmeli; rejim
   yalnızca izinli zenginleşmeyle değişebilir (SHORT_SQUEEZE→SHORT_TASFIYE,
   LONG_TASFIYE→LONG_KAPITULASYON).
2) Aile-eşleme kanıtı: v7.3'e surec_rejim=SHORT_TASFIYE/LONG_KAPITULASYON vermek,
   v7.2'ye es-ailesini vermekle aynı skorları üretmeli.
3) SÜPÜRME durum makinesi sentetik senaryoları (spec §9 tablosu).
"""
import ast, random, subprocess, datetime, calendar, time, sys, os

# v7.9: mutlak yol yerine dosyanin kendi konumu — test artik repo nereye
# klonlanirsa klonlansin calisir (git show da ayni dizinde kosar).
REPO = os.path.dirname(os.path.abspath(__file__))

def yukle(kaynak, isimler):
    tree = ast.parse(kaynak)
    ns = {'datetime': datetime, 'calendar': calendar, 'time': time}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in isimler['fn']:
            exec(ast.get_source_segment(kaynak, node), ns)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in isimler['sabit']:
            exec(ast.get_source_segment(kaynak, node), ns)
    return ns

SABITLER = {'SINYAL_ESIGI','SINYAL_MARJI','MALIYET_CITASI_PCT','VE_ISLEM_MIN',
            'VE_DIRENC_MIN','VE_DUVAR_MIN','EMIR_OLGUNLUK_SANIYE',
            'TASFIYE_AYRIMI_AKTIF','TASFIYE_DIKEN_CARPANI','TASFIYE_OI_MIN_PCT',
            'SUPURME_TESPIT_AKTIF','SEVIYE_LOOKBACK_DK','SEVIYE_KORUMA_DK',
            'SEVIYE_KUMELEME_VOL','SEVIYE_PIVOT_PENCERE_DK','SUPURME_YAKINLIK_VOL',
            'SUPURME_MIN_DELME_VOL','SUPURME_MAX_DELME_VOL','SUPURME_GERI_ALIM_MAX_DK',
            'SUPURME_GECERLILIK_DK','SUPURME_COOLDOWN_DK','KAPITULASYON_CARPANI',
            # v7.4:
            'EMILIM_AYRIMI_AKTIF','EMILIM_OLCUM_AKTIF','EMILIM_MIN_AKIS',
            'EMILIM_GUCLU_ESIK','EMILIM_YOK_ESIK','EMILIM_SPOT_ESIGI',
            'TUKENME_DILIM_SAYISI','TUKENME_SONME_ORANI','TUKENME_MAX_DUSUS_VOL','PENCERE_DK',
            # v7.5/v7.6 (v7.8: EMILIM_YONU_AKTIF kaldirildi — olu salterdi):
            'TUKENME_DILIM_DK','TUKENME_MIN_AKIS',
            'SPOT_OB_MAX_YAS_SN','EMILIM_EGILIM_ESIGI','EMILIM_DERINLIK_PCT',
            # v7.7:
            'PERP_OB_MAX_YAS_SN',
            # v8.0 swing seviye haritasi:
            'SWING_SEVIYE_AKTIF','SWING_ONCELIK','SWING_ROUND_ADIM','SWING_ROUND_MENZIL_VOL',
            'SWING_VP_KOVA_VOL','SWING_VP_DEGER_ALANI','SWING_LIQ_KOVA_VOL',
            'SWING_LIQ_MIN_KAT',
            # v8.1 FAZ B swing motoru:
            'SWING_MOTOR_AKTIF','SCALP_SINYAL_AKTIF','SWING_YAKINLIK_VOL','SWING_MIN_RR',
            'SWING_STOP_TAMPON_VOL','SWING_FUNDING_ASIRI','SWING_YAPISAL',
            # v8.2 FAZ C kayit:
            'SWING_ARSIV_AKTIF','SWING_UFUKLAR'}
FONKSIYONLAR = {'_norm','_olgunluk_carpani','_cvd_iraksama_hesapla',
                'ceyreklik_expiry_yakin_mi','balina_skoru_hesapla','supurme_takip_et',
                '_tasfiye_bayraklari',
                '_emilim_esnekligi','_emilim_borsasi',
                '_akis_tukenmesi','_cvd_kaynagi_tutarli',
                '_swing_seviye_haritasi',
                '_emici_yon','_swing_kademe','_swing_hedef_stop',
                '_swing_backtest','_grab_ozeti'}  # v7.4/v7.6/v7.8/v8.0/v8.1/v8.2

# SABITLENMIS taban: v7.2 = e6ee0ac. ("HEAD" kullanmak, v7.3 commit'lendikten
# sonra testi kendi-kendiyle kiyasa dusurup KORULUK etmez hale getirirdi —
# dogrulayici tespiti.)
# v7.9 (tasinabilirlik): taban ONCE git-ref'ten okunur; bu repo'nun tarihcesinde
# o commit YOKSA (kod baska repoya kopyalandi — orn. balina-avcisi-core) yani
# sabitlenmis v72_taban_main.py DOSYASINDAN okunur. Ikisi de ayni donmus v7.2
# kaynagidir; frozen-taban garantisi korunur, git tarihcesine bagimlilik biter.
V72_COMMIT = 'e6ee0ac'
try:
    eski_kaynak = subprocess.run(['git','show',f'{V72_COMMIT}:main.py'],capture_output=True,
                                 text=True,cwd=REPO).stdout
except (FileNotFoundError, OSError):
    eski_kaynak = ''   # v8.7: git binary'si olmayan makinede dosya-yedege dus (crash degil)
if not eski_kaynak:
    _taban_dosya = os.path.join(REPO, 'v72_taban_main.py')
    if os.path.exists(_taban_dosya):
        eski_kaynak = open(_taban_dosya).read()
assert eski_kaynak, "v7.2 taban ne git commit ('e6ee0ac') ne de v72_taban_main.py dosyasi olarak bulundu"
yeni_kaynak = open(os.path.join(REPO,'main.py')).read()
ESKI = yukle(eski_kaynak, {'fn':FONKSIYONLAR,'sabit':SABITLER})
YENI = yukle(yeni_kaynak, {'fn':FONKSIYONLAR,'sabit':SABITLER})
assert YENI['TASFIYE_AYRIMI_AKTIF'] is False, "FAZ 1 bayragi ACIK olamaz!"

fails=[]
def check(ad,c,d=""):
    print(f"[{'PASS' if c else 'FAIL'}] {ad}{('  '+d) if d else ''}")
    if not c: fails.append(ad)

# ---------- 1) 500 RASTGELE GİRDİ EŞDEĞERLİĞİ ----------
random.seed(73)
V72_REJIMLER = ['NOTR','TEPE_DAGITIM','DIP_TOPLAMA','SHORT_SQUEEZE','LONG_TASFIYE',
                'TAZE_ALIM','TAZE_SATIS']
IZINLI = {'SHORT_SQUEEZE':{'SHORT_SQUEEZE','SHORT_TASFIYE','TASFIYE_SONRASI_DONUS'},
          'LONG_TASFIYE':{'LONG_TASFIYE','LONG_KAPITULASYON'},
          # v7.4: DIP_TOPLAMA emilim-zenginlesmesi (izinli — skor/sinyal degismez)
          'DIP_TOPLAMA':{'DIP_TOPLAMA','DIP_TOPLAMA_SPOT','DIP_TOPLAMA_TEYITSIZ','DIP_TOPLAMA_PERP'},
          # v7.6: TEPE_DAGITIM emilim-zenginlesmesi (simetrik)
          'TEPE_DAGITIM':{'TEPE_DAGITIM','TEPE_DAGITIM_SPOT','TEPE_DAGITIM_TEYITSIZ','TEPE_DAGITIM_PERP'}}
farkli=0; rejim_zengin=0; dip_kapsama=0   # v7.4: DIP_TOPLAMA_* yoluna giren
for i in range(500):
    esik_d = random.uniform(1e7, 1e8)
    a = {
        'fiyat': random.uniform(50000,70000),
        'bid_d': random.uniform(0, esik_d*2), 'ask_d': random.uniform(0, esik_d*2),
        'bnb_delta': random.uniform(-.5,.5), 'byb_delta': random.uniform(-.5,.5),
        'okx_delta': random.uniform(-.5,.5), 'aktif_borsa': random.choice([1,2,3]),
        'vadeli_cvd': random.uniform(-5e5,5e5), 'spot_cvd': random.uniform(-3e6,3e6),
        'oi': random.uniform(1e9,2e10), 'funding': random.uniform(-.001,.001),
        'bid_yas': random.uniform(0,600), 'ask_yas': random.uniform(0,600),
        'likid': random.uniform(0,5e6), 'esik_d': esik_d,
        'esik_l': random.uniform(1e5,1e6),
        'esik_c_neg': -random.uniform(1e5,6e5), 'esik_c_poz': random.uniform(1e5,6e5),
        'surec_rejim': random.choice(V72_REJIMLER),
        'surec_tukenme': random.randint(0,4),
        'en_yakin_ask_fiyat': random.choice([0, random.uniform(60000,70500)]),
        'en_yakin_bid_fiyat': random.choice([0, random.uniform(49500,60000)]),
        # v7.3 girdileri (eski fonksiyon bunlari YOK SAYAR — a.get yok, dict fazlasi zararsiz)
        'tasfiye_long_yogunluk': random.choice([0.0, 1.0, 3.5, 8.0]),
        'tasfiye_short_yogunluk': random.choice([0.0, 1.0, 3.5, 8.0]),
        'esik_volatilite': random.uniform(0.02, 0.4),
        'supurme_dip_aktif': random.choice([True,False]),
        'supurme_tepe_aktif': random.choice([True,False]),
        # v7.4 girdileri (eski fonksiyon YOK SAYAR)
        'esik_spot_neg': -random.uniform(8e5, 3e6),
        'satici_tukenmesi': random.choice([True, False]),
        'sonme_orani': random.choice([None, 0.3, 0.9]),
    }
    pencere = None if random.random()<0.05 else {
        'd_fiyat_pct': random.uniform(-.6,.6),
        'd_vadeli_cvd': random.uniform(-1.5e6,1.5e6),
        'd_spot_cvd': random.uniform(-5e6,5e6),
        'd_oi_pct': random.uniform(-.6,.6),
        'cvd_iraksama': random.uniform(-1,1),
    }
    kalite = {'cvd_guvenilir': random.random()>0.1, 'sebep':'test'}
    e = ESKI['balina_skoru_hesapla'](dict(a), dict(pencere) if pencere else None, dict(kalite))
    y = YENI['balina_skoru_hesapla'](dict(a), dict(pencere) if pencere else None, dict(kalite))
    if (round(e[0],6),round(e[1],6),e[2]) != (round(y[0],6),round(y[1],6),y[2]):
        farkli+=1
        if farkli<=3: print("  FARK:",e[:3],"vs",y[:3])
    if e[3]!=y[3]:
        if y[3] in IZINLI.get(e[3],set()): rejim_zengin+=1
        else:
            farkli+=1
            if farkli<=3: print("  REJIM IHLALI:",e[3],"->",y[3])
    if str(y[3]).startswith('DIP_TOPLAMA_'): dip_kapsama+=1
check("500 girdide skor+sinyal BIREBIR ayni", farkli==0, f"fark={farkli}, izinli rejim zenginlesmesi={rejim_zengin}")
# KAPSAMA (spec §8.2): sifirsa test BOS gecmistir -> gecersiz
check("v7.4 DIP_TOPLAMA_* yolu GERCEKTEN calisti (kapsama>0)", dip_kapsama>0,
      f"dip_kapsama={dip_kapsama}/500")

# ---------- 2) AILE EŞLEME: yeni adlar es-aileleriyle ayni davranir ----------
def skorla(ns, surec_rejim, fn='balina_skoru_hesapla'):
    a = {'fiyat':60000,'bid_d':6e7,'ask_d':3e7,'bnb_delta':.1,'byb_delta':.1,'okx_delta':.1,
         'aktif_borsa':3,'vadeli_cvd':-2e5,'spot_cvd':-1e6,'oi':1.2e10,'funding':0.0001,
         'bid_yas':400,'ask_yas':100,'likid':2e5,'esik_d':4.5e7,'esik_l':2e5,
         'esik_c_neg':-3e5,'esik_c_poz':3e5,'surec_rejim':surec_rejim,'surec_tukenme':1,
         'en_yakin_ask_fiyat':0,'en_yakin_bid_fiyat':0,
         'tasfiye_long_yogunluk':0.0,'tasfiye_short_yogunluk':0.0,'esik_volatilite':0.1}
    pencere = {'d_fiyat_pct':0.05,'d_vadeli_cvd':-4e5,'d_spot_cvd':-1e6,'d_oi_pct':0.0,'cvd_iraksama':0.5}
    return ns[fn](a,pencere,{'cvd_guvenilir':True,'sebep':'ok'})
e_sq = ESKI['balina_skoru_hesapla'] and skorla(ESKI,'SHORT_SQUEEZE')
y_st = skorla(YENI,'SHORT_TASFIYE')
y_sq = skorla(YENI,'SHORT_SQUEEZE')
check("surec_rejim=SHORT_TASFIYE == v7.2 SHORT_SQUEEZE", e_sq[:3]==y_st[:3]==y_sq[:3])
e_lt = skorla(ESKI,'LONG_TASFIYE'); y_lk = skorla(YENI,'LONG_KAPITULASYON')
check("surec_rejim=LONG_KAPITULASYON == v7.2 LONG_TASFIYE", e_lt[:3]==y_lk[:3])
y_tsd = skorla(YENI,'TASFIYE_SONRASI_DONUS')
check("surec_rejim=TASFIYE_SONRASI_DONUS == v7.2 SHORT_SQUEEZE (FAZ 1)", e_sq[:3]==y_tsd[:3])

# ---------- 3) SÜPÜRME DURUM MAKİNESİ (spec §9 senaryolari) ----------
sup = YENI['supurme_takip_et']
SEV=[{'fiyat':58000.0,'test':3,'yas_dk':400}]
VOL=0.1
def tik(D,fiyat,fitil,kap,tas,t):
    return sup(D,SEV,False,fiyat,fitil,VOL,kap,tas,t)

# S1: delme + geri alim YOK -> zaman asimi -> OLU
D={}; t=1000.0
tik(D,58050,58050,False,False,t)                 # SILAHLI
tik(D,57950,57900,False,False,t+60)              # DELINDI (0.172% > 0.03%)
check("S1a: DELINDI", D[58000]['durum']=='DELINDI')
tik(D,57950,57950,False,False,t+60+16*60)        # 16dk gecti, geri alim yok
check("S1b: geri alimsiz zaman asimi -> OLU", D[58000]['durum']=='OLU')

# S2: cok derin delme -> KIRILMA -> OLU (8 x 0.1% = 0.8%; 57000 = 1.72%)
D={}; t=2000.0
tik(D,58050,58050,False,False,t)
tik(D,57900,57000,True,True,t+60)
check("S2: derin delme = KIRILMA -> OLU", D[58000]['durum']=='OLU')

# S3: fitil + geri alim AMA tasfiye dikeni YOK -> ONAYLI DEGIL
D={}; t=3000.0
tik(D,58050,58050,False,False,t)
tik(D,57950,57900,True,False,t+60)
a3,_=tik(D,58010,58010,True,False,t+120)
check("S3: tasfiye teyidi eksik -> ONAYLI degil", D[58000]['durum']=='DELINDI' and a3 is None)

# S4: fitil + geri alim + TUM teyitler -> ONAYLI + yeni onay kaydi
D={}; t=4000.0
tik(D,58050,58050,False,False,t)
tik(D,57950,57900,True,True,t+60)
a4,on4=tik(D,58010,58010,True,True,t+120)
check("S4: tam kurulum -> ONAYLI", D[58000]['durum']=='ONAYLI' and a4 is not None and len(on4)==1)
check("S4b: fitil ucu dogru", on4 and on4[0]['fitil_uc']==57900)

# S5: cooldown — ONAYLI gecerlilik (30dk) bitince OLU'ya, cooldown (60dk) bitince BEKLEME'ye
a5,_=tik(D,58200,58200,False,False,t+120+31*60)
check("S5a: gecerlilik doldu -> OLU + aktif yok", D[58000]['durum']=='OLU' and a5 is None)
tik(D,58200,58200,False,False,t+120+31*60+61*60)
check("S5b: cooldown bitti -> BEKLEME", D[58000]['durum']=='BEKLEME')

# S6: vol=0 -> hicbir gecis yok
D={}; r6=sup(D,SEV,False,58050,57000,0.0,True,True,9000.0)
check("S6: vol=0 -> tespit yapilmaz", r6==(None,[]) and D=={})

# S7: seviye listesi bos -> sorunsuz
D={}; r7=sup(D,[],False,58050,57000,VOL,True,True,9100.0)
check("S7: seviye yok -> sorunsuz bos donus", r7==(None,[]))

# S8: TEPE simetrisi — yukari fitil + asagi geri alim + teyitler -> ONAYLI
D={}; t=5000.0; SEVT=[{'fiyat':62000.0,'test':2,'yas_dk':300}]
sup(D,SEVT,True,61950,61950,VOL,False,False,t)       # SILAHLI (yaklasti)
sup(D,SEVT,True,62050,62150,VOL,True,True,t+60)      # DELINDI (fitil yukari 0.24%)
a8,on8=sup(D,SEVT,True,61990,61990,VOL,True,True,t+120)
check("S8: tepe supurmesi ONAYLI", D[62000]['durum']=='ONAYLI' and len(on8)==1)

# S9: KANONIK GECIKMELI SUPURME (1 Tem deseni) — kapitulasyon+diken FITILDE,
# geri alim 8dk sonra (o anda 5dk pencereler sonmus: kap=False, tas=False).
# LATCH sayesinde yine ONAYLI olmali. (Eski ayni-dakika sarti bunu KACIRIYORDU.)
D={}; t=6000.0
tik(D,58050,58050,False,False,t)                 # SILAHLI
tik(D,57950,57900,True,True,t+60)                # DELINDI; kap+tas LATCH fitilde
tik(D,57960,57940,False,False,t+240)             # hala altta; teyitler sonmus
a9,on9=tik(D,58020,58020,False,False,t+540)      # 8dk sonra geri alim, teyitsiz an
check("S9: gecikmeli geri alim LATCH ile ONAYLI", D[58000]['durum']=='ONAYLI' and len(on9)==1,
      f"geri_alim_dk={on9[0]['geri_alim_dk'] if on9 else '—'}")

# S10: SEVIYE KAYMASI — adaptif yenileme medyani kaydirir; durum YENIDEN
# ANAHTARLANMALI (yetim SILAHLI/DELINDI kalmamali, taze ikiz dogmamali).
D={}; t=7000.0
tik(D,58050,58050,False,False,t)                 # SILAHLI @58000
tik(D,57950,57900,True,True,t+60)                # DELINDI
SEV_KAYMIS=[{'fiyat':58011.0,'test':4,'yas_dk':410}]  # medyan kaydi (1xvol icinde)
sup(D,SEV_KAYMIS,False,57960,57950,VOL,False,False,t+120)
check("S10a: kayan seviyeye tek durum tasindi", list(D.keys())==[58011],
      f"keys={sorted(D.keys())}")
check("S10b: DELINDI ilerlemesi korundu", D[58011]['durum']=='DELINDI'
      and D[58011]['kap_ts']>0 and D[58011]['tas_ts']>0)
a10,on10=sup(D,SEV_KAYMIS,False,58030,58030,VOL,False,False,t+300)
check("S10c: kaymis seviyede latch'li onay calisiyor", D[58011]['durum']=='ONAYLI' and len(on10)==1)

# ---------- 4) TASFIYE AYRIMI: FAZ 1'de veto AYNEN durur ----------
a_t = {'fiyat':60000,'bid_d':6e7,'ask_d':3e7,'bnb_delta':.1,'byb_delta':.1,'okx_delta':.1,
       'aktif_borsa':3,'vadeli_cvd':-2e5,'spot_cvd':-1e6,'oi':1.2e10,'funding':0.0001,
       'bid_yas':400,'ask_yas':100,'likid':2e5,'esik_d':4.5e7,'esik_l':2e5,
       'esik_c_neg':-3e5,'esik_c_poz':3e5,'surec_rejim':'NOTR','surec_tukenme':0,
       'en_yakin_ask_fiyat':0,'en_yakin_bid_fiyat':0,
       'tasfiye_long_yogunluk':5.0,'tasfiye_short_yogunluk':0.0,'esik_volatilite':0.1}
p_t = {'d_fiyat_pct':-0.10,'d_vadeli_cvd':-8e5,'d_spot_cvd':-2e6,'d_oi_pct':-0.30,'cvd_iraksama':0.4}
e_r = ESKI['balina_skoru_hesapla'](dict(a_t),dict(p_t),{'cvd_guvenilir':True,'sebep':'ok'})
y_r = YENI['balina_skoru_hesapla'](dict(a_t),dict(p_t),{'cvd_guvenilir':True,'sebep':'ok'})
check("T1: zorla long tasfiyesi -> rejim LONG_KAPITULASYON", y_r[3]=='LONG_KAPITULASYON', f"eski={e_r[3]}")
check("T2: FAZ 1'de skorlar birebir ayni (veto duruyor)", e_r[:3]==y_r[:3])

# ---------- 5) v7.3.1 GERÇEKLİK TESTLERİ ----------
# G1 — 1 Tem kanonik supurmesinin GERI ALIM bari (dogrulanmis piyasa vakasi):
# fiyat +0.15%, OI -0.35% (LONG flush), LONG diken 8.0x, SHORT diken 0.
# Spec kurali: her spec, dogru siniflandirmasi ZORUNLU bir gercek vaka icerir.
a_g1 = {'fiyat':60000,'bid_d':6e7,'ask_d':3e7,'bnb_delta':.1,'byb_delta':.1,
        'okx_delta':.1,'aktif_borsa':3,'vadeli_cvd':-2e5,'spot_cvd':-1e6,'oi':1.2e10,
        'funding':0.0001,'bid_yas':400,'ask_yas':100,'likid':2e5,'esik_d':4.5e7,
        'esik_l':2e5,'esik_c_neg':-3e5,'esik_c_poz':3e5,'surec_rejim':'NOTR',
        'surec_tukenme':0,'en_yakin_ask_fiyat':0,'en_yakin_bid_fiyat':0,
        'tasfiye_long_yogunluk':8.0,'tasfiye_short_yogunluk':0.0,'esik_volatilite':0.11}
p_g1 = {'d_fiyat_pct':0.15,'d_oi_pct':-0.35,'d_vadeli_cvd':-9e5,'d_spot_cvd':-3e6,
        'cvd_iraksama':0.5}
Lg,Sg,sigg,rejg,_,_ = YENI['balina_skoru_hesapla'](dict(a_g1),dict(p_g1),{'cvd_guvenilir':True,'sebep':'ok'})
check("G1a: kanonik geri-alim bari SHORT_SQUEEZE DEGIL", rejg!='SHORT_SQUEEZE', f"rejim={rejg}")
check("G1a2: dogru etiket TASFIYE_SONRASI_DONUS", rejg=='TASFIYE_SONRASI_DONUS')
# G1b: GERCEK kod yolu (_tasfiye_bayraklari) dogru hucreyi secmeli — totoloji degil,
# ana dongudeki fonksiyonun kendisi cagriliyor.
tv, ty = YENI['_tasfiye_bayraklari'](8.0, 0.0, -0.35)
check("G1b: kanonik supurmede tasfiye_var=True (2x2 dogru hucre)", tv is True and ty=='LONG',
      f"tasfiye_var={tv} yon={ty}")
tv2, _ = YENI['_tasfiye_bayraklari'](8.0, 0.0, +0.10)   # OI dusmedi -> tasfiye degil
check("G1c: OI dususu olmadan diken tek basina tasfiye SAYILMAZ", tv2 is False)

# G2 — FAZ 2 ANLAMLILIK: flag acikken sinyal GERCEKTEN cikmali (NO-OP korumasi).
FAZ2 = yukle(yeni_kaynak, {'fn':FONKSIYONLAR,'sabit':SABITLER})
FAZ2['TASFIYE_AYRIMI_AKTIF'] = True
for sr in ('NOTR','SHORT_TASFIYE','TASFIYE_SONRASI_DONUS'):
    a = dict(a_g1); a['surec_rejim']=sr
    L2,S2,sig2,rej2,_,_ = FAZ2['balina_skoru_hesapla'](a,dict(p_g1),{'cvd_guvenilir':True,'sebep':'ok'})
    check(f"G2: FAZ2 surec_rejim={sr} -> LONG (NO-OP degil)", sig2=='LONG',
          f"long={L2:.1f} sinyal={sig2}")
# G2-negatif: FAZ 2'de bile GONULLU squeeze (diken yok) veto + aile korunur
a_n = dict(a_g1); a_n['tasfiye_long_yogunluk']=0.0; a_n['surec_rejim']='SHORT_SQUEEZE'
L3,S3,sig3,rej3,_,_ = FAZ2['balina_skoru_hesapla'](a_n,dict(p_g1),{'cvd_guvenilir':True,'sebep':'ok'})
check("G2n: FAZ2'de gonullu SHORT_SQUEEZE hala vetolu (BEKLE)", sig3=='BEKLE' and rej3=='SHORT_SQUEEZE',
      f"long={L3:.1f} sinyal={sig3} rejim={rej3}")

# ---------- 6) v7.4 REFERANS VAKALARI (§7) + None yayilimi ----------
# Gercek veri esikleri (578 kayittan olculdu): esik_c=382K, esik_s=1.73M, vol=0.022
E_C, E_S, E_VOL = -381834.0, -1731424.0, 0.0221
esnek = YENI['_emilim_esnekligi']; bors = YENI['_emilim_borsasi']
# v7.8: sarmalayici kaldirildi — test GERCEK uretim giris noktasini cagirir
tuk = lambda seri, c, s, v: YENI['_akis_tukenmesi'](seri, 'SATIS', c, s, v)
YOK, GUCLU = YENI['EMILIM_YOK_ESIK'], YENI['EMILIM_GUCLU_ESIK']

# None YAYILIMI (§9.2 sifir tuzagi): akis yoksa None, 0.0 DEGIL
check("E-None: akis yok -> esneklik None (0.0 DEGIL)", esnek(0.0,0,0,E_C,E_S,E_VOL) is None)
# v7.5: _emilim_borsasi 4-tuple (borsa, spot_eg, perp_eg, spot_pay); akis+defter yok -> hepsi None
check("E-None: akis+defter yok -> borsa 4-tuple hepsi None (0.0 DEGIL)",
      bors(0,0,E_C,E_S)==(None,None,None,None))
# v7.5: GERCEK spot defter — spot bid-agir -> 'SPOT' (envanter alimi teyitli)
b_sp = bors(-4e5,-1.5e6,E_C,E_S, spot_bid_d=8e6, spot_ask_d=2e6,
            perp_bid_d=5e7, perp_ask_d=5e7, spot_ob_yasi_sn=30)
check("E-defter: spot bid-agir -> borsa 'SPOT'", b_sp[0]=='SPOT' and b_sp[1] is not None, f"={b_sp}")
b_ap = bors(-4e5,-1.5e6,E_C,E_S, spot_bid_d=2e6, spot_ask_d=8e6,
            perp_bid_d=5e7, perp_ask_d=5e7, spot_ob_yasi_sn=30)
check("E-defter: spot ask-agir -> spot_egilim NEGATIF (dagitim teyidi)", b_ap[1] < 0, f"={b_ap}")
# v7.5: defter BAYAT -> v7.4 vekiline dus ('_AKIS' son eki ile).
# spot-baskin akis (spot_pay>=EMILIM_SPOT_ESIGI) -> 'SPOT_AKIS' (defterden DEGIL).
b_st = bors(-2e5,-4e6,E_C,E_S, spot_bid_d=8e6, spot_ask_d=2e6,
            perp_bid_d=5e7, perp_ask_d=5e7, spot_ob_yasi_sn=9999)
check("E-defter: bayat defter -> '_AKIS' vekiline duser", str(b_st[0]).endswith('_AKIS'), f"={b_st}")

# VAKA 1 — NEGATIF (en kritik): tavan reddi, -1.24% cakilma, spot -20M vadeli -3K.
# Sistem bunu ASLA emilim saymamali -> esneklik > EMILIM_YOK_ESIK.
e_v1 = esnek(-1.24, -3000, -20e6, E_C, E_S, E_VOL)
check("V1: tavan reddi EMILIM DEGIL (esneklik > YOK_ESIK)", e_v1 > YOK, f"esneklik={e_v1:.2f}")
# daha yumusak bir versiyonda da (yarim hareket) emilim sayilmamali
check("V1b: -0.8% cakilma da emilim degil", esnek(-0.8,-3000,-20e6,E_C,E_S,E_VOL) > YOK)

# VAKA 2 — POZITIF: buyuk emilim, duz fiyat, satis SONMUYOR -> DIP_TOPLAMA_TEYITSIZ.
# Once metrik: esneklik DUSUK (< GUCLU), sonra tam balina_skoru_hesapla ile etiket.
e_v2 = esnek(-0.01, -6e5, -2e6, E_C, E_S, E_VOL)
check("V2: guclu emilim (esneklik < GUCLU_ESIK)", e_v2 < GUCLU, f"esneklik={e_v2:.3f}")
a_v2 = {'fiyat':60000,'bid_d':8e7,'ask_d':2e7,'bnb_delta':.1,'byb_delta':.1,'okx_delta':.1,
        'aktif_borsa':3,'vadeli_cvd':-6e5,'spot_cvd':-2e6,'oi':1.2e10,'funding':0.0001,
        'bid_yas':400,'ask_yas':100,'likid':2e5,'esik_d':4.5e7,'esik_l':2e5,
        'esik_c_neg':E_C,'esik_c_poz':3e5,'surec_rejim':'NOTR','surec_tukenme':0,
        'en_yakin_ask_fiyat':0,'en_yakin_bid_fiyat':0,'tasfiye_long_yogunluk':0.0,
        'tasfiye_short_yogunluk':0.0,'esik_volatilite':E_VOL,'esik_spot_neg':E_S,
        'satici_tukenmesi':False,'sonme_orani':0.94}
p_v2 = {'d_fiyat_pct':0.01,'d_vadeli_cvd':-6e5,'d_spot_cvd':-2e6,'d_oi_pct':0.0,'cvd_iraksama':0.5}
L2,S2,sig2,rej2,ac2,em2 = YENI['balina_skoru_hesapla'](dict(a_v2),dict(p_v2),{'cvd_guvenilir':True,'sebep':'ok'})
check("V2: satis sonmeyen guclu emilim -> DIP_TOPLAMA_TEYITSIZ (SISTEM 'BELIRSIZ' DER)",
      rej2=='DIP_TOPLAMA_TEYITSIZ', f"rejim={rej2} esneklik={em2['emilim_esnekligi']}")
e_v2b = ESKI['balina_skoru_hesapla'](dict(a_v2),dict(p_v2),{'cvd_guvenilir':True,'sebep':'ok'})
check("V2b: (long,short,sinyal) v7.2 ile BIREBIR (olcum skoru ETKILEMEZ)",
      e_v2b[:3]==(L2,S2,sig2), f"v72={e_v2b[:3]} v74={(L2,S2,sig2)}")

# VAKA 2T — SIMETRIK (v7.6): TEPE_DAGITIM tarafi. Agresif ALIS emiliyor, fiyat
# tutmuyor, ask-duvar agir -> base rejim TEPE_DAGITIM. alici_tuk YOK -> _TEYITSIZ;
# alici_tuk VAR + spot ASK-agir taze defter -> _SPOT. V2 (DIP) ile birebir ayna.
a_t = {'fiyat':60000,'bid_d':2e7,'ask_d':8e7,'bnb_delta':-.1,'byb_delta':-.1,'okx_delta':-.1,
       'aktif_borsa':3,'vadeli_cvd':6e5,'spot_cvd':2e6,'oi':1.2e10,'funding':0.0001,
       'bid_yas':100,'ask_yas':400,'likid':2e5,'esik_d':4.5e7,'esik_l':2e5,
       'esik_c_neg':E_C,'esik_c_poz':3e5,'surec_rejim':'NOTR','surec_tukenme':0,
       'en_yakin_ask_fiyat':0,'en_yakin_bid_fiyat':0,'tasfiye_long_yogunluk':0.0,
       'tasfiye_short_yogunluk':0.0,'esik_volatilite':E_VOL,'esik_spot_neg':E_S,
       'alici_tukenmesi':False,'alici_sonme_orani':0.94}
p_t = {'d_fiyat_pct':-0.01,'d_vadeli_cvd':6e5,'d_spot_cvd':2e6,'d_oi_pct':0.0,'cvd_iraksama':0.5}
Lt,St,sigt,rejt,act,emt = YENI['balina_skoru_hesapla'](dict(a_t),dict(p_t),{'cvd_guvenilir':True,'sebep':'ok'})
check("V2T: alici tukenmeyen guclu emilim -> TEPE_DAGITIM_TEYITSIZ (simetri)",
      rejt=='TEPE_DAGITIM_TEYITSIZ', f"rejim={rejt} esneklik={emt['emilim_esnekligi']}")
e_tb = ESKI['balina_skoru_hesapla'](dict(a_t),dict(p_t),{'cvd_guvenilir':True,'sebep':'ok'})
check("V2Tb: (long,short,sinyal) v7.2 ile BIREBIR (TEPE_DAGITIM_* es-aile)",
      e_tb[:3]==(Lt,St,sigt), f"v72={e_tb[:3]} v76={(Lt,St,sigt)}")
# alici_tuk VAR + spot ASK-agir taze defter -> _SPOT (gercek dagitim, alici tukeniyor)
a_ts = dict(a_t); a_ts.update({'alici_tukenmesi':True,'spot_bid_d':2e6,'spot_ask_d':8e6,
                               'spot_ob_yasi_sn':30})
_,_,_,rej_ts,_,_ = YENI['balina_skoru_hesapla'](dict(a_ts),dict(p_t),{'cvd_guvenilir':True,'sebep':'ok'})
check("V2Ts: alici tukendi + spot ASK-agir taze defter -> TEPE_DAGITIM_SPOT",
      rej_ts=='TEPE_DAGITIM_SPOT', f"rejim={rej_ts}")

# VAKA 2P — v7.7: PERP mutabakati emilim dict'ine YANSIR ama skoru ETKILEMEZ.
# Perp defteri zaten 3 borsa toplaniyordu; simetrik mutabakat sayaci eklendi.
a_p = dict(a_v2); a_p.update({'perp_borsa_sayisi':3,'perp_bid_agir_sayi':2,'perp_ask_agir_sayi':0})
Lp,Sp,sigp,rejp,acp,emp = YENI['balina_skoru_hesapla'](dict(a_p),dict(p_v2),{'cvd_guvenilir':True,'sebep':'ok'})
check("V2P: perp mutabakati emilim dict'ine yansir (3 borsa, 2 bid-agir)",
      emp['perp_borsa_sayisi']==3 and emp['perp_bid_agir_sayi']==2 and emp['perp_ask_agir_sayi']==0,
      f"={emp['perp_borsa_sayisi']}/{emp['perp_bid_agir_sayi']}/{emp['perp_ask_agir_sayi']}")
check("V2P: perp mutabakati (long,short,sinyal,rejim) ETKILEMEZ (olcum-only)",
      (Lp,Sp,sigp,rejp)==(L2,S2,sig2,rej2), f"perp={(Lp,Sp,sigp,rejp)} baz={(L2,S2,sig2,rej2)}")
# Girdi hic YOKKEN de emilim dict guvenli varsayilan (0) doner (KeyError DEGIL)
check("V2P: perp girdisi yoksa emilim dict 0 doner (KeyError degil)",
      em2['perp_borsa_sayisi']==0 and em2['perp_bid_agir_sayi']==0)

# VAKA 3 — REGRESYON: 1 Tem kanonik supurmesi hala TASFIYE_SONRASI_DONUS + tasfiye_var.
tv3,_ = YENI['_tasfiye_bayraklari'](8.0, 0.0, -0.35)
check("V3: 1 Tem regresyonu — tasfiye_var hala True", tv3 is True)
# (rejim TASFIYE_SONRASI_DONUS zaten G1a2'de dogrulandi)

# AKIS TUKENMESI fonksiyonu (v7.6 yonlu) — sonen seri True, sabit seri False.
# v7.6: dilim = TUKENME_DILIM_DK(15dk); yon SATIS=negatif CVD, ALIS=pozitif CVD.
akis_tuk = YENI['_akis_tukenmesi']
def seri_yap(akislar, yon='SATIS'):
    # akislar[0]=EN ESKI dilim -> son=EN YENI. Her dilim TUKENME_DILIM_DK dk;
    # dilim ici birikimli CVD, yone gore isaretli (SATIS=asagi, ALIS=yukari).
    W = YENI['TUKENME_DILIM_DK']*60
    isaret = -1.0 if yon=='SATIS' else 1.0
    seri=[]; t0=1_000_000.0; cvd=0.0
    for di,ak in enumerate(akislar):        # di=0 en eski dilim
        bas=t0 + di*W
        seri.append({'ts':bas+1,     'spot_cvd':cvd, 'vadeli_cvd':0.0, 'fiyat':60000})
        cvd += isaret*ak*abs(E_S)
        seri.append({'ts':bas+W-1,   'spot_cvd':cvd, 'vadeli_cvd':0.0, 'fiyat':60000})
    return sorted(seri,key=lambda r:r['ts'])
# --- SATIS yonu (satici tukenmesi -> toplama imzasi) ---
tv_son,orn_son = tuk(seri_yap([1.0,0.6,0.3]), E_C, E_S, E_VOL)   # sonuyor: 0.3/1.0=0.3<0.5
check("E-tuk: sonen satis + duz fiyat -> tukenme True", tv_son is True, f"sonme={orn_son}")
tv_sbt,_ = tuk(seri_yap([1.0,1.0,1.0]), E_C, E_S, E_VOL)         # sabit: 1.0/1.0=1.0
check("E-tuk: sabit satis -> tukenme False", tv_sbt is False)
tv_az,orn_az = tuk([{'ts':1,'spot_cvd':0,'vadeli_cvd':0,'fiyat':60000}], E_C, E_S, E_VOL)
check("E-tuk: yetersiz veri -> (False, None) (0.0 DEGIL)", tv_az is False and orn_az is None)
# --- ALIS yonu (alici tukenmesi -> dagitim/tepe imzasi) — SATIS ile SIMETRIK ---
av_son,ao_son = akis_tuk(seri_yap([1.0,0.6,0.3],'ALIS'),'ALIS', E_C, E_S, E_VOL)
check("E-tuk(ALIS): sonen alis + duz fiyat -> alici tukenmesi True", av_son is True, f"sonme={ao_son}")
av_sbt,_ = akis_tuk(seri_yap([1.0,1.0,1.0],'ALIS'),'ALIS', E_C, E_S, E_VOL)
check("E-tuk(ALIS): sabit alis -> tukenme False", av_sbt is False)
# YON AYRIMI: sonen SATIS serisi ALIS yonunden bakildiginda tukenme SAYILMAZ
# (v7.4 abs()'i bunlari ayirt EDEMEZDI — v7.6 yonlu ayrimi bunu ispatliyor).
av_yanlis,_ = akis_tuk(seri_yap([1.0,0.6,0.3],'SATIS'),'ALIS', E_C, E_S, E_VOL)
check("E-tuk: yon ayrimi (sonen SATIS != alici tukenmesi)", av_yanlis is False, f"={av_yanlis}")

# ---------- 7) v7.8: CVD KAYNAK TUTARLILIGI (FIX1 sinifi) ----------
# Coinalyze<->WS gecisinde taban ziplar; karisik-kaynakli pencerede delta
# gurultudur. Koruma: pencere icinde TEK kaynak yoksa olcum atlanir.
kt = YENI['_cvd_kaynagi_tutarli']
s_homo = [{'ts':1000.0+i*60,'cvd_kaynak':'AGG'} for i in range(10)]
check("K1: tek kaynak -> tutarli", kt(s_homo, 600) is True)
s_mix = [dict(r) for r in s_homo]; s_mix[7]['cvd_kaynak']='WS'
check("K2: pencere icinde kaynak gecisi -> TUTARSIZ", kt(s_mix, 600) is False)
# pencere DISINDA kalan eski gecis SAYILMAZ (sadece bakilan pencere onemli)
s_eski = [{'ts':100.0,'cvd_kaynak':'WS'}] + [{'ts':5000.0+i*60,'cvd_kaynak':'AGG'} for i in range(5)]
check("K3: pencere disi eski gecis sayilmaz", kt(s_eski, 240) is True)
check("K4: bos seri -> False (olculemez, 0.0/True UYDURULMAZ)", kt([], 600) is False)

# ---------- 8) v8.0: SWING SEVIYE HARITASI (scalp'tan AYRI kod yolu) ----------
# SAF fonksiyon: coklu kaynak -> tek liste, priority-merge. Skoru ETKILEMEZ
# (bu blok balina_skoru_hesapla'yi hic cagirmaz; esdegerlik yukarida fark=0).
import math as _m
ssh = YENI['_swing_seviye_haritasi']
_t0 = 1_000_000.0
_seri = [(_t0 + i*60, 62000 + 800*_m.sin(i/120)
          + (600 if 700 < i < 720 else 0) - (1000 if 200 < i < 210 else 0))
         for i in range(1500)]                                   # ~25 saat
_dip = [{'fiyat':61000.0,'test':4,'yas_dk':300}]
_tep = [{'fiyat':63500.0,'test':3,'yas_dk':500}]
_liq = [(61050.0,5e6,0),(61040.0,4e6,0),(63480.0,0,3e6),(62000.0,1e5,1e5)]
_elle = [{'fiyat':61010.0,'not':'benim dip'}]                    # 61000 pivotuna 1xvol icinde
_out = ssh(62000.0, _seri, 0.15, _dip, _tep, _liq, _elle)
_gor = [s for s in _out if not s['gizli']]
_kaynaklar = set(s['kaynak'] for s in _gor)
check("W1: coklu kaynak uretildi (SWING_PIVOT/HL/ROUND/VP)",
      {'ROUND','VP','HL'} <= _kaynaklar and len(_gor) > 0, f"={sorted(_kaynaklar)}")
# A3: elle seviye, yakin pivotu/round'u GIZLER; kendisi GORUNUR kalir + oncelik 0
_yakin = [s for s in _out if 60950 < s['fiyat'] < 61080]
_elle_gor = [s for s in _yakin if s['kaynak']=='ELLE']
_pivot_giz = [s for s in _yakin if s['kaynak']=='SWING_PIVOT' and s['gizli']]
check("W2 (A3): elle seviye gorunur + oncelik 0",
      len(_elle_gor)==1 and _elle_gor[0]['gizli'] is False and _elle_gor[0]['oncelik']==0)
check("W3 (A3): 1xvol icindeki oto pivot GIZLENDI (elle KAZANDI)",
      len(_pivot_giz)>=1, f"yakin={[(s['kaynak'],s['gizli']) for s in _yakin]}")
# oncelik dogru sirali: ELLE(0) < VP(1) < HL(2) < LIQ(3) < SWING_PIVOT(4) < ROUND(5)
check("W4: oncelik haritasi dogru", YENI['SWING_ONCELIK']['ELLE']==0
      and YENI['SWING_ONCELIK']['ROUND']==5 and YENI['SWING_ONCELIK']['VP']<YENI['SWING_ONCELIK']['HL'])
# vol=0 -> VP/LIQ (vol-bagimli) ATLANIR; vol-bagimsizlar (ELLE/HL/ROUND/PIVOT) uretilir
_vz = [s for s in ssh(62000.0, _seri, 0.0, _dip, _tep, _liq, _elle) if not s['gizli']]
_vz_k = set(s['kaynak'] for s in _vz)
check("W5: vol=0 -> VP/LIQ atlanir, digerleri uretilir",
      'VP' not in _vz_k and 'LIQ' not in _vz_k and 'ROUND' in _vz_k, f"={sorted(_vz_k)}")
# SIFIR TUZAGI: bos girdi -> bos liste (sahte seviye UYDURULMAZ)
check("W6: bos girdi -> bos liste (sahte seviye uydurulmaz)", ssh(0,[],0,[],[],[],[])==[])
# round-number'lar gercekten yuvarlak ($1000 kati) ve anlik fiyat menzilinde
_round = [s['fiyat'] for s in _out if s['kaynak']=='ROUND']
check("W7: ROUND seviyeler $1000 kati", all(abs(r % 1000.0) < 0.5 for r in _round) and len(_round)>=2, f"={_round[:5]}")

# ---------- 9) v8.1 FAZ B: KADEMELI SWING MOTORU (scalp'tan AYRI) ----------
# SAF fonksiyonlar; balina_skoru_hesapla'yi HIC cagirmaz (fark=0 yukarida korunur).
kdm = YENI['_swing_kademe']; hst = YENI['_swing_hedef_stop']; eyn = YENI['_emici_yon']
_emL = {'emilim_esnekligi':0.2,'satici_tukenmesi':True,'alici_tukenmesi':False,'spot_egilim':0.3}
_emS = {'emilim_esnekligi':0.2,'satici_tukenmesi':False,'alici_tukenmesi':True,'spot_egilim':-0.3}
check("SB0: emici_yon LONG/SHORT/None dogru",
      eyn(_emL)=='LONG' and eyn(_emS)=='SHORT' and eyn({}) is None and eyn({'emilim_esnekligi':2.0}) is None)
_sev = [{'fiyat':61000.0,'kaynak':'SWING_PIVOT','gizli':False},
        {'fiyat':62000.0,'kaynak':'ROUND','gizli':False},
        {'fiyat':63500.0,'kaynak':'HL','gizli':False,'not':'dun H'},
        {'fiyat':63624.0,'kaynak':'VP','gizli':False},
        {'fiyat':60480.0,'kaynak':'LIQ','gizli':False,'hacim':9e7}]
_vol=0.15
_g0={'yon':None,'baslad':False,'tamam':False}
_gL={'yon':'LONG','baslad':True,'tamam':True}
_gS={'yon':'SHORT','baslad':True,'tamam':True}
check("SB1: seviye yok -> kademe YOK", kdm(62000,[],_vol,{},False,{},0.0001,0.0)['kademe']=='YOK')
_izle=kdm(61150,_sev,_vol,_g0,False,{},0.0001,0.0)
check("SB2: seviyeye 2xvol yakin, sensor yok -> IZLE (skor 25)",
      _izle['kademe']=='IZLE' and _izle['kademe_skoru']==25, f"={_izle['kademe']}/{_izle['kademe_skoru']}")
_haz=kdm(61150,_sev,_vol,_g0,False,_emL,0.0001,0.0)
check("SB3: + emici basladi -> HAZIRLAN", _haz['kademe']=='HAZIRLAN' and _haz['yon']=='LONG')
_sinL=kdm(61150,_sev,_vol,_gL,True,_emL,0.0001,0.0)
check("SB4: 4/4 (seviye+grab+tasfiye+emici) uyumlu -> SINYAL LONG skor 100",
      _sinL['kademe']=='SINYAL' and _sinL['yon']=='LONG' and _sinL['kademe_skoru']==100,
      f"={_sinL['kademe']}/{_sinL['yon']}/{_sinL['kademe_skoru']}")
_sinS=kdm(63450,_sev,_vol,_gS,True,_emS,0.0001,0.0)
check("SB5: direnc altinda 4/4 SHORT -> SINYAL SHORT", _sinS['kademe']=='SINYAL' and _sinS['yon']=='SHORT')
# YON CELISKISI: grab LONG ama emici SHORT (gercek yonlu sensorler celisir) -> SINYAL YOK.
# (v8.4: seviye_yon yon uzlasisindan cikarildi; celiski artik SADECE grab<->emici'den.)
_cel=kdm(61150,_sev,_vol,_gL,True,_emS,0.0001,0.0)
check("SB6: grab<->emici yon celiskisi -> SINYAL DEGIL (yon=None)",
      _cel['kademe']!='SINYAL' and _cel['yon'] is None, f"={_cel['kademe']}/{_cel['yon']}")
# UZLASMA: grab SHORT + emici SHORT, seviye altta (eski seviye_yon LONG olurdu) ->
# artik seviye_yon karismaz -> SHORT SINYAL (eski kod bunu YANLISLIKLA bloklardi)
_uzl=kdm(61150,_sev,_vol,_gS,True,_emS,0.0001,0.0)
check("SB6b: grab+emici SHORT uzlasti (seviye konumu karismaz) -> SINYAL SHORT",
      _uzl['kademe']=='SINYAL' and _uzl['yon']=='SHORT', f"={_uzl['kademe']}/{_uzl['yon']}")
# uzak seviye -> IZLE'ye bile girmez (mesafe>2xvol)
_uzak=kdm(62800,_sev,_vol,_gL,True,_emL,0.0001,0.0)
check("SB7: seviye 2xvol'den uzak -> kademe YOK (yakin degil)", _uzak['kademe']=='YOK')
# --- _swing_hedef_stop ---
_hS=hst('SHORT',63450,_sev,_vol,magnet=60480.0)
check("SB8: SHORT hedef/stop yapidan; stop YAPISAL (63500 HL, ROUND degil) + tampon",
      _hS['kisa_hedef']==62000.0 and _hS['swing_hedef']==60480.0 and 63500 < _hS['stop'] < 63600
      and _hS['gecerli'] is True, f"={_hS}")
check("SB9: rr_swing hesabi dogru (giris-swing)/(stop-giris)",
      abs(_hS['rr_swing'] - (63450-60480)/(_hS['stop']-63450)) < 0.1)
_hL=hst('LONG',61150,_sev,_vol,magnet=None)
check("SB10: LONG simetrik; stop ALT yapisal (61000 pivot) - tampon, gecerli",
      _hL['kisa_hedef']==62000.0 and _hL['stop'] < 61000 and _hL['gecerli'] is True)
# R/R vetosu: swing hedef cok yakin -> rr_swing<1.5 -> gecerli False
_bad=hst('SHORT',62050,[{'fiyat':62000.0,'kaynak':'HL','gizli':False},
                        {'fiyat':63624.0,'kaynak':'VP','gizli':False}],_vol,magnet=62000.0)
check("SB11: rr_swing < SWING_MIN_RR -> gecerli False (kotu R/R -> sinyal yok)",
      _bad['gecerli'] is False and _bad['rr_swing'] < YENI['SWING_MIN_RR'])
check("SB12: yon yok / yapisal eksik -> gecerli False (uydurmaz)",
      hst(None,62000,_sev,_vol)['gecerli'] is False
      and hst('SHORT',62000,[{'fiyat':61000.0,'kaynak':'ROUND','gizli':False}],_vol)['gecerli'] is False)
# BAYRAKLAR: swing acik, scalp susturuldu; Faz 1 hala kapali
check("SB13: SWING_MOTOR_AKTIF=True, SCALP_SINYAL_AKTIF=False (scalp sustu)",
      YENI['SWING_MOTOR_AKTIF'] is True and YENI['SCALP_SINYAL_AKTIF'] is False)
# AYRI KOD YOLU ISPATI: swing fonksiyonlari balina_skoru_hesapla cagirmaz
import ast as _ast
_src = open(os.path.join(REPO,'main.py')).read()
_swing_fn = [n for n in _ast.walk(_ast.parse(_src)) if isinstance(n,_ast.FunctionDef)
             and n.name in ('_swing_kademe','_swing_hedef_stop','_emici_yon')]
_cagrilar = set()
for _fn in _swing_fn:
    for _n in _ast.walk(_fn):
        if isinstance(_n,_ast.Call) and isinstance(_n.func,_ast.Name): _cagrilar.add(_n.func.id)
check("SB14: swing fonksiyonlari SKOR fonksiyonu cagirmaz (ayri kod yolu)",
      'balina_skoru_hesapla' not in _cagrilar and 'supurme_takip_et' not in _cagrilar,
      f"cagirdiklari={sorted(_cagrilar)}")

# ---------- 10) v8.2 FAZ C: SWING KOHORT GERI-TESTI (scalp'tan AYRI) ----------
import datetime as _dt
bt = YENI['_swing_backtest']
UF = YENI['SWING_UFUKLAR']
_t0 = 2_000_000.0
def _seri_yon(bas, adim, n=600):    # dogrusal fiyat serisi (dakikada 1)
    return [(_t0 + i*60, bas + i*adim) for i in range(n)]
_z = _dt.datetime.utcfromtimestamp(_t0 + 60).isoformat()   # sinyal: 2. kayit
# WIN: SHORT, fiyat DUSUYOR -> hedef 61000 once vurulur (stop 64000 hic)
_win_seri = _seri_yon(63450, -15)         # dik dusus: 4s icinde 61000 vurulur
_o_win = [{'zaman':_z,'yon':'SHORT','swing_hedef':61000.0,'stop':64000.0,'rr_swing':3.0}]
_r_win = bt(_o_win, _win_seri, UF)['4s']
check("SC1: SHORT hedef ONCE vuruldu -> WIN + ort_r=+rr",
      _r_win['win']==1 and _r_win['loss']==0 and _r_win['isabet']==100.0 and _r_win['ort_r']==3.0,
      f"={_r_win}")
# LOSS: SHORT, fiyat YUKSELIYOR -> stop 64000 once vurulur (hedef 61000 hic)
_loss_seri = _seri_yon(63450, +5)
_o_loss = [{'zaman':_z,'yon':'SHORT','swing_hedef':61000.0,'stop':64000.0,'rr_swing':3.0}]
_r_loss = bt(_o_loss, _loss_seri, UF)['4s']
check("SC2: SHORT stop ONCE vuruldu -> LOSS + ort_r=-1",
      _r_loss['loss']==1 and _r_loss['win']==0 and _r_loss['isabet']==0.0 and _r_loss['ort_r']==-1.0,
      f"={_r_loss}")
# ACIK: olay serinin SONUNA yakin -> uzun ufuk (3g) henuz dolmadi + hedef/stop uzak
_z_yeni = _dt.datetime.utcfromtimestamp(_win_seri[-1][0]-120).isoformat()
_o_acik = [{'zaman':_z_yeni,'yon':'LONG','swing_hedef':999999.0,'stop':1.0,'rr_swing':3.0}]
_r_acik = bt(_o_acik, _win_seri, UF)
check("SC3: cozulmemis olay -> ACIK (win/loss'a sayilmaz)",
      _r_acik['3g']['acik']==1 and _r_acik['3g']['n']==0 and _r_acik['3g']['isabet'] is None)
# LONG simetrik WIN: fiyat yukseliyor -> hedef ustte vurulur
_o_long = [{'zaman':_z,'yon':'LONG','swing_hedef':64000.0,'stop':61000.0,'rr_swing':2.0}]
_r_long = bt(_o_long, _seri_yon(63450,+5), UF)['4s']
check("SC4: LONG simetrik -> hedef ust vuruldu WIN", _r_long['win']==1 and _r_long['ort_r']==2.0)
# 4 ufuk uretiliyor + dayaniklilik
check("SC5: 4 ufuk (4s/12s/1g/3g) + bos girdi guvenli",
      [e for e,_ in UF]==['4s','12s','1g','3g'] and bt([], _win_seri, UF)['4s']['n']==0
      and bt(_o_win, [], UF)['4s']['n']==0)
check("SC6: SWING_ARSIV_AKTIF + ufuk sabiti dogru",
      YENI['SWING_ARSIV_AKTIF'] is True and len(YENI['SWING_UFUKLAR'])==4)

# ---------- 11) v8.7: DENETIM DUZELTMELERI ----------
go = YENI['_grab_ozeti']; GECER = YENI['SUPURME_GECERLILIK_DK']*60
_now=1_000_000.0
# SILAHLI baslad SAYILMAZ (denetim 2/2 KESIN: HAZIRLAN pivot yakininda surekli aciliyordu)
check("GO1: SILAHLI tek basina -> baslad False (yakinlik IZLE'nin isi)",
      go({58000:{'durum':'SILAHLI'}},{},_now)=={'yon':None,'baslad':False,'tamam':False})
check("GO2: DELINDI -> baslad True, tamam False, yon LONG(dip)",
      go({58000:{'durum':'DELINDI'}},{},_now)=={'yon':'LONG','baslad':True,'tamam':False})
check("GO3: taze ONAYLI -> tamam True",
      go({58000:{'durum':'ONAYLI','onay_ts':_now-60}},{},_now)['tamam'] is True)
# yetim/bayat ONAYLI (gecerlilik doldu) tamam SAYILMAZ (denetim 2/2 KESIN)
check("GO4: bayat ONAYLI (gecerlilik doldu) -> tamam False, baslad False",
      go({58000:{'durum':'ONAYLI','onay_ts':_now-GECER-60}},{},_now)=={'yon':None,'baslad':False,'tamam':False})
check("GO5: tepe DELINDI -> yon SHORT",
      go({},{62000:{'durum':'DELINDI'}},_now)['yon']=='SHORT')
check("GO6: dip+tepe ayni anda aktif -> yon None (celiski, SINYAL kapanir)",
      go({58000:{'durum':'DELINDI'}},{62000:{'durum':'DELINDI'}},_now)['yon'] is None)
# funding_asiri dali (100x funding hatasinin regresyon kapisi)
_fh=kdm(61150,_sev,_vol,_g0,False,{},0.001,0.0)
check("SB15: funding asiri (0.001>0.0005) tek basina -> HAZIRLAN + sebep",
      _fh['kademe']=='HAZIRLAN' and any('funding' in s for s in _fh['sebepler']))
_fn=kdm(61150,_sev,_vol,_g0,False,{},0.0001,0.0)
check("SB16: normal funding (0.0001) -> IZLE (HAZIRLAN tetiklenmez)", _fn['kademe']=='IZLE')
# SHORT + magnet=None -> swing_hedef en uzak ALT seviye
_hm=hst('SHORT',63450,_sev,_vol,magnet=None)
check("SB17: SHORT magnet=None -> swing_hedef en uzak alt seviye (60480)",
      _hm['swing_hedef']==60480.0 and _hm['gecerli'] is True, f"={_hm['swing_hedef']}")
# W8: LIQ kaynagi pozitif assert. Kume esigi MEDYAN-kovanin katidir; gercekci
# arka-plan (cok sayida kucuk kova) olmadan medyan buyur ve kume gecemez —
# ilk fixtur bu yuzden kusurluydu. 40 kucuk arka-plan + 1 buyuk kume.
_liq_arka=[(60000.0+i*137.0, 1e4, 1e4) for i in range(40)]
_w8=ssh(62000.0,_seri,0.15,[],[],_liq_arka+[(58200.0,9e6,0)],[])
check("W8: LIQ kumesi gorunur uretildi (pozitif kanit)",
      any(s['kaynak']=='LIQ' and not s['gizli'] and abs(s['fiyat']-58200)<200 for s in _w8),
      f"LIQ={[s['fiyat'] for s in _w8 if s['kaynak']=='LIQ']}")
# _swing_backtest TZ bagimsizligi: naive zaman UTC varsayilir (Istanbul'da da ayni sonuc)
import os as _os
check("SC7: backtest naive zamani UTC varsayar (fixtur utcfromtimestamp ile uyumlu)",
      bt(_o_win,_win_seri,UF)['4s']['win']==1)

print()
print("HEPSI GECTI" if not fails else f"BASARISIZ: {fails}")
sys.exit(1 if fails else 0)
