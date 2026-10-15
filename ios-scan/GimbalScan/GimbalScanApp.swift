import RealityKit
import SwiftUI

@main
struct GimbalScanApp: App {
    @StateObject private var link = Link()

    var body: some SwiftUI.Scene {
        WindowGroup {
            RootView(link: link)
                .onOpenURL { url in link.pair(with: url.absoluteString) }
        }
    }
}

// MARK: - Strings (6 languages, same as the PC panel)

enum Str {
    static var lang: String { UserDefaults.standard.string(forKey: "lang") ?? "tr" }

    static let table: [String: [String: String]] = [
        "tr": ["scan_qr": "Bilgisayardaki 3D Tarama QR kodunu okut", "waiting": "Bilgisayar bekleniyor", "connected": "Bilgisayar bağlı",
               "room": "Oda planı", "mesh": "Detaylı mesh", "start": "Taramayı başlat", "stop": "Bitir", "processing": "Model hazırlanıyor…",
               "sent": "Model bilgisayara gönderildi ✔", "send_failed": "Gönderilemedi", "repair": "Yeniden eşle", "unsupported": "Bu cihazda LiDAR yok",
               "move_close": "Duvara yaklaş", "move_away": "Duvardan uzaklaş", "slow_down": "Daha yavaş", "more_light": "Daha fazla ışık lazım", "low_texture": "Desensiz yüzey — açıyı değiştir",
               "hint": "PC'den “Otomatik 360°” başlatabilir ya da gimballe odada yavaşça dolaşabilirsin."],
        "en": ["scan_qr": "Scan the 3D Scan QR code on your computer", "waiting": "Waiting for computer", "connected": "Computer connected",
               "room": "Room plan", "mesh": "Detailed mesh", "start": "Start scan", "stop": "Finish", "processing": "Building model…",
               "sent": "Model sent to computer ✔", "send_failed": "Send failed", "repair": "Re-pair", "unsupported": "This device has no LiDAR",
               "move_close": "Move closer to the wall", "move_away": "Move away from the wall", "slow_down": "Slow down", "more_light": "More light needed", "low_texture": "Featureless surface — change angle",
               "hint": "Start “Auto 360°” from the PC, or walk slowly around the room with the gimbal."],
        "es": ["scan_qr": "Escanea el código QR de Escaneo 3D del ordenador", "waiting": "Esperando al ordenador", "connected": "Ordenador conectado",
               "room": "Plano", "mesh": "Malla detallada", "start": "Iniciar escaneo", "stop": "Terminar", "processing": "Creando modelo…",
               "sent": "Modelo enviado ✔", "send_failed": "Error al enviar", "repair": "Volver a emparejar", "unsupported": "Este dispositivo no tiene LiDAR",
               "move_close": "Acércate a la pared", "move_away": "Aléjate de la pared", "slow_down": "Más despacio", "more_light": "Se necesita más luz", "low_texture": "Superficie lisa: cambia el ángulo",
               "hint": "Inicia «Auto 360°» desde el PC o recorre la habitación despacio con el gimbal."],
        "ru": ["scan_qr": "Отсканируйте QR-код 3D-сканирования на компьютере", "waiting": "Ожидание компьютера", "connected": "Компьютер подключён",
               "room": "План комнаты", "mesh": "Детальная сетка", "start": "Начать", "stop": "Завершить", "processing": "Создание модели…",
               "sent": "Модель отправлена ✔", "send_failed": "Ошибка отправки", "repair": "Переподключить", "unsupported": "На этом устройстве нет LiDAR",
               "move_close": "Подойдите ближе к стене", "move_away": "Отойдите от стены", "slow_down": "Медленнее", "more_light": "Нужно больше света", "low_texture": "Однородная поверхность — смените угол",
               "hint": "Запустите «Авто 360°» с ПК или медленно обойдите комнату со стабилизатором."],
        "zh": ["scan_qr": "扫描电脑上的 3D 扫描二维码", "waiting": "等待电脑连接", "connected": "电脑已连接",
               "room": "房间平面", "mesh": "精细网格", "start": "开始扫描", "stop": "完成", "processing": "正在生成模型…",
               "sent": "模型已发送到电脑 ✔", "send_failed": "发送失败", "repair": "重新配对", "unsupported": "此设备没有 LiDAR",
               "move_close": "靠近墙壁", "move_away": "远离墙壁", "slow_down": "放慢速度", "more_light": "需要更多光线", "low_texture": "表面缺少纹理——换个角度",
               "hint": "可在电脑上启动“自动 360°”，或手持云台在房间内慢慢走动。"],
        "ar": ["scan_qr": "امسح رمز QR الخاص بالمسح ثلاثي الأبعاد على الحاسوب", "waiting": "في انتظار الحاسوب", "connected": "الحاسوب متصل",
               "room": "مخطط الغرفة", "mesh": "شبكة مفصّلة", "start": "بدء المسح", "stop": "إنهاء", "processing": "جارٍ إنشاء النموذج…",
               "sent": "تم إرسال النموذج ✔", "send_failed": "فشل الإرسال", "repair": "إعادة الإقران", "unsupported": "لا يحتوي هذا الجهاز على LiDAR",
               "move_close": "اقترب من الجدار", "move_away": "ابتعد عن الجدار", "slow_down": "أبطئ", "more_light": "يلزم ضوء أكثر", "low_texture": "سطح بلا نقوش — غيّر الزاوية",
               "hint": "ابدأ «تلقائي 360°» من الحاسوب أو تجوّل ببطء في الغرفة مع المثبّت."],
    ]

    static func t(_ key: String) -> String {
        table[lang]?[key] ?? table["en"]?[key] ?? key
    }
}

// MARK: - Views

struct RootView: View {
    @ObservedObject var link: Link
    @AppStorage("lang") private var lang = "tr"   // re-renders all text when the PC changes language

    var body: some View {
        Group {
            if link.paired {
                ScanView(link: link)
            } else {
                ZStack(alignment: .bottom) {
                    QRScanner { code in link.pair(with: code) }
                        .ignoresSafeArea()
                    Text(Str.t("scan_qr"))
                        .font(.headline)
                        .padding(.horizontal, 18).padding(.vertical, 12)
                        .background(.ultraThinMaterial, in: Capsule())
                        .padding(.bottom, 40)
                }
            }
        }
        .environment(\.layoutDirection, lang == "ar" ? .rightToLeft : .leftToRight)
        .onAppear { UIApplication.shared.isIdleTimerDisabled = true }
    }
}

struct ARPreview: UIViewRepresentable {
    let arView: ARView
    func makeUIView(context: Context) -> ARView { arView }
    func updateUIView(_ uiView: ARView, context: Context) {}
}

struct ScanView: View {
    @ObservedObject var link: Link
    @StateObject private var scanner: Scanner
    @AppStorage("lang") private var lang = "tr"

    init(link: Link) {
        self.link = link
        _scanner = StateObject(wrappedValue: Scanner(link: link))
    }

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            ARPreview(arView: scanner.arView).ignoresSafeArea()

            VStack(spacing: 10) {
                HStack(spacing: 8) {
                    pill(color: link.connected ? .green : .orange,
                         text: link.connected ? "\(Str.t("connected")) · \(link.host)" : Str.t("waiting"))
                    Spacer()
                    Button(Str.t("repair")) { scanner.stop(); link.unpair() }
                        .font(.caption.bold())
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .background(.ultraThinMaterial, in: Capsule())
                }
                if !scanner.instruction.isEmpty {
                    pill(color: .yellow, text: Str.t(scanner.instruction))
                }
                if !scanner.supported {
                    pill(color: .red, text: Str.t("unsupported"))
                }
                Spacer()
                if scanner.busy {
                    HStack(spacing: 8) { ProgressView(); Text(Str.t("processing")) }
                        .padding(12).background(.ultraThinMaterial, in: Capsule())
                } else if !scanner.message.isEmpty {
                    pill(color: .green, text: Str.t(scanner.message))
                }
                if !scanner.scanning && !scanner.busy {
                    Text(Str.t("hint")).font(.caption).multilineTextAlignment(.center)
                        .padding(10).background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 10))
                }
                HStack(spacing: 14) {
                    Picker("", selection: $scanner.mode) {
                        Text(Str.t("room")).tag(Scanner.Mode.room)
                        Text(Str.t("mesh")).tag(Scanner.Mode.mesh)
                    }
                    .pickerStyle(.segmented)
                    .frame(maxWidth: 260)
                    .disabled(scanner.scanning || scanner.busy)

                    Button {
                        scanner.scanning ? scanner.stop() : scanner.start()
                    } label: {
                        Text(scanner.scanning ? Str.t("stop") : Str.t("start"))
                            .font(.headline)
                            .padding(.horizontal, 22).padding(.vertical, 14)
                            .background(scanner.scanning ? Color.red : Color.blue, in: Capsule())
                            .foregroundColor(.white)
                    }
                    .disabled(scanner.busy || !scanner.supported)
                }
            }
            .padding(16)
            .foregroundColor(.white)
        }
        .onAppear {
            link.onConnected = { [weak scanner] in
                link.send(json: ["type": "hello", "device": UIDevice.current.name])
                scanner?.sendState()
            }
            link.onCommand = { [weak scanner] c in
                guard let scanner else { return }
                switch c["cmd"] as? String {
                case "start":
                    let m = Scanner.Mode(rawValue: c["mode"] as? String ?? "") ?? scanner.mode
                    scanner.start(m)
                case "stop":
                    scanner.stop()
                case "mode":
                    if !scanner.scanning, let m = Scanner.Mode(rawValue: c["value"] as? String ?? "") { scanner.mode = m }
                    scanner.sendState()
                case "lang":
                    if let l = c["value"] as? String, l != lang { lang = l }
                default:
                    break
                }
            }
            if !link.connected { link.reconnect() }
        }
    }

    private func pill(color: Color, text: String) -> some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 8, height: 8)
            Text(text).font(.caption.bold()).lineLimit(1)
        }
        .padding(.horizontal, 10).padding(.vertical, 6)
        .background(.ultraThinMaterial, in: Capsule())
    }
}
