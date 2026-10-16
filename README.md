# iPhone Gimbal Cam

iPhone 15 Pro Max'i Windows'ta yüksek kaliteli webcam olarak kullanmak ve Zhiyun Crane M2S gimbali bilgisayardan (ve yüz/vücut takibiyle) yönetmek için.

## Parçalar

| Klasör | Ne yapar |
|---|---|
| `windows/` | **Ana uygulama.** Ekranda QR gösterir; iPhone Safari ile bağlanır (uygulama kurmadan). 720p canlı önizleme, kafa/vücut takibi, gimbal kontrolü, sanal webcam; telefonda 4K30 kayıt ve otomatik aktarım (`Videolar\GimbalCam`) |
| `ios/` | İsteğe bağlı yerli iPhone kamera uygulaması (4K60 kayıt için ileride) |
| `tools/gimbal-test.html` | Tarayıcıdan (Chrome/Edge) gimbal test aracı |

## Gimbal protokolü (Crane M2S, firmware 1.73)

- BLE servis `fee9`, yazma `d44bc439-abfd-45a2-b575-925416129600` (write without response), bildirim `...9601`
- Paket: `24 3C | uzunluk (LE, 2B) | 18 12 | sıra | 01 | komut | veri | CRC16-XMODEM (LE)`
- Joystick: komut = eksen (01 tilt, 02 pan, 03 roll), veri = `10` + değer (LE, 0–4095, merkez 2048)
- Sorgu 04 → firmware ×100 (173 = 1.73)

## Kullanım

1. [Releases → latest](../../releases/tag/latest) sayfasından `GimbalCam-windows.zip` indir, çıkar, `GimbalCam.exe` çalıştır. Windows Güvenlik Duvarı sorarsa **Özel ağlar**a izin ver.
2. Ekrandaki QR kodu iPhone Kamera ile tarat. Safari ilk seferde “bağlantı gizli değil” der: *Ayrıntıları Göster → bu web sitesini ziyaret et*.
3. **Kamerayı başlat** → görüntü bilgisayardaki panelde. Gimbale bağlan, takibi aç.
   - Kısayollar: ok tuşları = gimbal, **Esc = DURDUR**, R = kayıt, P = duraklat, 1/2/3 = takip modu.
   - Dil: Türkçe, English, Español, Русский, 中文, العربية (sağ üstten).
   - **Webcam kartı:** görüntü 720p/1080p · 30/60 fps, **akıllı kadraj** (yüzü dijital zoom ile kadrajda tutar), ayna, **el hareketleri** (avuç içi 1 sn = takip aç/kapa, yumruk 1 sn = durdur).
   - Sanal kamera sabit hızda çalışır; telefon koparsa Zoom/Teams donmaz, “telefon bekleniyor” ekranı görünür ve telefon dönünce kendiliğinden devam eder.
   - Gimbal **Otomatik bağlan** açıkken uygulama açılınca bağlanır, koparsa yeniden dener.
4. Sanal webcam için [OBS Studio](https://obsproject.com) kurulu olmalı; Zoom/Teams'te “OBS Virtual Camera” seç.

## iOS derleme

Her push'ta GitHub Actions uygulamayı imzasız derler (`Actions → iOS build → GimbalCam-ipa`). `.ipa` dosyası Sideloadly ile ücretsiz Apple ID kullanılarak yüklenir.
