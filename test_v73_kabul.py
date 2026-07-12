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
import ast, random, subprocess, datetime, calendar, time, sys

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
            'SUPURME_GECERLILIK_DK','SUPURME_COOLDOWN_DK','KAPITULASYON_CARPANI'}
FONKSIYONLAR = {'_norm','_olgunluk_carpani','_cvd_iraksama_hesapla',
                'ceyreklik_expiry_yakin_mi','balina_skoru_hesapla','supurme_takip_et',
                '_tasfiye_bayraklari'}

# SABITLENMIS taban: v7.2 = e6ee0ac. ("HEAD" kullanmak, v7.3 commit'lendikten
# sonra testi kendi-kendiyle kiyasa dusurup KORULUK etmez hale getirirdi —
# dogrulayici tespiti.)
V72_COMMIT = 'e6ee0ac'
eski_kaynak = subprocess.run(['git','show',f'{V72_COMMIT}:main.py'],capture_output=True,
                             text=True,cwd='/home/user/balina-app').stdout
assert eski_kaynak, "v7.2 taban commit'i bulunamadi"
yeni_kaynak = open('/home/user/balina-app/main.py').read()
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
          'LONG_TASFIYE':{'LONG_TASFIYE','LONG_KAPITULASYON'}}
farkli=0; rejim_zengin=0
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
check("500 girdide skor+sinyal BIREBIR ayni", farkli==0, f"fark={farkli}, izinli rejim zenginlesmesi={rejim_zengin}")

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
Lg,Sg,sigg,rejg,_ = YENI['balina_skoru_hesapla'](dict(a_g1),dict(p_g1),{'cvd_guvenilir':True,'sebep':'ok'})
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
    L2,S2,sig2,rej2,_ = FAZ2['balina_skoru_hesapla'](a,dict(p_g1),{'cvd_guvenilir':True,'sebep':'ok'})
    check(f"G2: FAZ2 surec_rejim={sr} -> LONG (NO-OP degil)", sig2=='LONG',
          f"long={L2:.1f} sinyal={sig2}")
# G2-negatif: FAZ 2'de bile GONULLU squeeze (diken yok) veto + aile korunur
a_n = dict(a_g1); a_n['tasfiye_long_yogunluk']=0.0; a_n['surec_rejim']='SHORT_SQUEEZE'
L3,S3,sig3,rej3,_ = FAZ2['balina_skoru_hesapla'](a_n,dict(p_g1),{'cvd_guvenilir':True,'sebep':'ok'})
check("G2n: FAZ2'de gonullu SHORT_SQUEEZE hala vetolu (BEKLE)", sig3=='BEKLE' and rej3=='SHORT_SQUEEZE',
      f"long={L3:.1f} sinyal={sig3} rejim={rej3}")

print()
print("HEPSI GECTI" if not fails else f"BASARISIZ: {fails}")
sys.exit(1 if fails else 0)
