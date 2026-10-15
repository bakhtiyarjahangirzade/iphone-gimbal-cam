import SwiftUI
import AVFoundation

@main
struct GimbalCamApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

struct ContentView: View {
    @StateObject private var camera = CameraController()

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            CameraPreview(session: camera.session)
                .ignoresSafeArea()

            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 10) {
                    Circle()
                        .fill(camera.clients > 0 ? Color.green : Color.orange)
                        .frame(width: 10, height: 10)
                    Text(camera.clients > 0 ? "Bilgisayar bağlı" : "Bağlantı bekleniyor")
                        .font(.headline)
                    Text("· \(camera.mode.name) · \(String(format: "%.0f", camera.fpsActual)) fps")
                        .font(.subheadline.monospacedDigit())
                }
                Text("Wi-Fi: \(camera.ipAddress):\(StreamServer.port)   ·   USB: kablo ile otomatik")
                    .font(.caption.monospacedDigit())
                    .opacity(0.8)
                if !camera.status.isEmpty {
                    Text(camera.status).font(.caption).foregroundColor(.yellow)
                }
                if camera.isRecording {
                    Label("KAYIT \(camera.recordingTime)", systemImage: "record.circle.fill")
                        .font(.caption.bold())
                        .foregroundColor(.red)
                }
                Spacer()
                HStack(spacing: 12) {
                    Picker("Mod", selection: Binding(
                        get: { camera.mode },
                        set: { camera.setMode($0) })) {
                        ForEach(StreamMode.all) { Text($0.name).tag($0) }
                    }
                    .pickerStyle(.segmented)
                    .frame(maxWidth: 340)

                    Picker("Lens", selection: Binding(
                        get: { camera.lens },
                        set: { camera.setLens($0) })) {
                        ForEach(Lens.allCases) { Text($0.title).tag($0) }
                    }
                    .pickerStyle(.segmented)
                    .frame(maxWidth: 200)

                    Spacer()

                    Button {
                        camera.toggleRecording()
                    } label: {
                        Image(systemName: camera.isRecording ? "stop.circle.fill" : "record.circle")
                            .font(.system(size: 40))
                            .foregroundColor(.red)
                    }
                }
            }
            .foregroundColor(.white)
            .padding(16)
            .shadow(color: .black.opacity(0.8), radius: 3)
        }
        .statusBarHidden(true)
        .persistentSystemOverlays(.hidden)
        .onAppear {
            UIApplication.shared.isIdleTimerDisabled = true
            camera.start()
        }
    }
}

struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession

    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var previewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
        override func layoutSubviews() {
            super.layoutSubviews()
            if let c = previewLayer.connection, c.isVideoRotationAngleSupported(0) {
                c.videoRotationAngle = 0
            }
        }
    }

    func makeUIView(context: Context) -> PreviewView {
        let v = PreviewView()
        v.previewLayer.session = session
        v.previewLayer.videoGravity = .resizeAspect
        return v
    }

    func updateUIView(_ uiView: PreviewView, context: Context) {}
}
