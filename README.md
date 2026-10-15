# iPhone Gimbal Cam

iPhone 15 Pro Max'i Windows'ta yüksek kaliteli webcam olarak kullanmak ve Zhiyun Crane M2S gimbali bilgisayardan (ve yüz/vücut takibiyle) yönetmek için.

## Parçalar

| Klasör | Ne yapar |
|---|---|
| `ios/` | iPhone uygulaması: kamera → H.264 canlı yayın (USB/Wi-Fi, port 5005), 4K60 HEVC kayıt ve bilgisayara aktarım |
| `windows/` | Windows uygulaması: görüntü alma, sanal webcam, gimbal kontrolü, takip *(yakında)* |
| `tools/gimbal-test.html` | Tarayıcıdan (Chrome/Edge) gimbal test aracı |

## Gimbal protokolü (Crane M2S, firmware 1.73)

- BLE servis `fee9`, yazma `d44bc439-abfd-45a2-b575-925416129600` (write without response), bildirim `...9601`
- Paket: `24 3C | uzunluk (LE, 2B) | 18 12 | sıra | 01 | komut | veri | CRC16-XMODEM (LE)`
- Joystick: komut = eksen (01 tilt, 02 pan, 03 roll), veri = `10` + değer (LE, 0–4095, merkez 2048)
- Sorgu 04 → firmware ×100 (173 = 1.73)

## iOS derleme

Her push'ta GitHub Actions uygulamayı imzasız derler (`Actions → iOS build → GimbalCam-ipa`). `.ipa` dosyası Sideloadly ile ücretsiz Apple ID kullanılarak yüklenir.
