# Veri Yedekleri

Tarihli Supabase anlık görüntüleri. Yedekler silinmez; her tazeleme YENİ
tarihli dosyalar ekler.

## 2026-07-30 (v9.2+v9.3 canlı dönemi — en zengin snapshot)

- `balina_avcisi_data_2026-07-30_parca1..4.csv` — **3.726 dakikalık satır,
  kesintisiz** (27 Tem 21:46 → 30 Tem 12:21 UTC), 67 kolon: core akış + swing
  + teşhis kolonları + `golge_*` (v9.3 gölge kayıtları dahil, ilk kayıt
  28 Tem 06:21). parca1 en eski, parca4 en yeni dilim.
- `balina_ayarlar_2026-07-30_500olay.sql` — 15 anahtar; `swing_kohortu` içinde
  **500 olay** (388 GRAB_ADAY + 112 GRAB_ADAY_N1 — teyit kapısı kalibrasyon
  verisi), `ve_kapisi_redleri`, `geri_test_istatistik`, `grab_aktif_sinyal`
  (22 Tem GRAB_DONUS sinyal kartı) vb.

Not: Bu snapshot alındığında 22 Tem GRAB_DONUS sinyali kohorttan eski budamayla
silinmiş haldeydi (v9.4 bunu düzeltti); kayıt `grab_aktif_sinyal` kartından
onarım SQL'iyle kohorta geri yüklendi.

## 2026-07-14 (eski — canlı veri silinmesi sonrası ilk yedek)

- `balina_ayarlar_2026-07-14_66olay.sql` — 6 anahtar; en kritiği `tasfiye_kohortu`
  içinde **66 Faz-1 kohort olayı** (yon/rejim/tasfiye_var/seviye/zaman). YERİ DOLDURULAMAZ.
- `balina_avcisi_data_2026-07-14_403satir.sql` — 403 dakikalık satır. 17 emilim kolonu
  ŞEMADA VAR ama tamamen NULL (arşiv kodu o an DEPLOY olmamıştı — kayıp değil, henüz
  yazılmıyordu).

## RESTORE NOTU

`balina_ayarlar` 'anahtar' bazlı — mevcut canlı anahtarlarla çakışmasın diye
UPSERT ya da önce-sil-sonra-ekle yap. `balina_avcisi_data` CSV'leri Supabase
Table Editor "Import data from CSV" ile ya da `\copy` ile geri yüklenebilir
(id çakışması varsa önce hedef aralığı temizle).
