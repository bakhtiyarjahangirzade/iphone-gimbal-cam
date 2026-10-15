import AVFoundation
import UIKit

struct StreamMode: Hashable, Identifiable {
    let name: String
    let width: Int32
    let height: Int32
    let fps: Int
    var id: String { name }

    static let all: [StreamMode] = [
        StreamMode(name: "1080p30", width: 1920, height: 1080, fps: 30),
        StreamMode(name: "1080p60", width: 1920, height: 1080, fps: 60),
        StreamMode(name: "4K30", width: 3840, height: 2160, fps: 30),
        StreamMode(name: "4K60", width: 3840, height: 2160, fps: 60),
    ]

    /// Default bitrate for the live stream (Mbps).
    var defaultBitrate: Double {
        switch (height, fps) {
        case (1080, 30): return 12
        case (1080, _): return 20
        case (_, 30): return 35
        default: return 50
        }
    }
}

enum Lens: String, CaseIterable, Identifiable {
    case ultra, wide, tele
    var id: String { rawValue }
    var title: String {
        switch self {
        case .ultra: return "0.5x"
        case .wide: return "1x"
        case .tele: return "5x"
        }
    }
    var deviceType: AVCaptureDevice.DeviceType {
        switch self {
        case .ultra: return .builtInUltraWideCamera
        case .wide: return .builtInWideAngleCamera
        case .tele: return .builtInTelephotoCamera
        }
    }
}

final class CameraController: NSObject, ObservableObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let session = AVCaptureSession()

    @Published var status = ""
    @Published var mode: StreamMode = StreamMode.all[1]
    @Published var lens: Lens = .wide
    @Published var clients = 0
    @Published var fpsActual = 0.0
    @Published var ipAddress = "—"
    @Published var isRecording = false
    @Published var recordingTime = "00:00"

    private let sessionQueue = DispatchQueue(label: "cam.session")
    private let videoQueue = DispatchQueue(label: "cam.video", qos: .userInteractive)
    private let output = AVCaptureVideoDataOutput()
    private var device: AVCaptureDevice?
    private var bitrateMbps: Double = 20

    private let encoder = VideoEncoder()
    private let server = StreamServer()
    private let recorder = Recorder()

    private var frameCount = 0
    private var fpsWindowStart = CACurrentMediaTime()
    private var started = false
    private var recordStart: Date?
    private var recordTimer: Timer?

    // MARK: - Start

    func start() {
        guard !started else { return }
        started = true
        ipAddress = NetworkInfo.wifiAddress() ?? "—"

        encoder.onEncoded = { [weak self] data, isKey, pts in
            self?.server.broadcastVideo(data, keyframe: isKey, ptsMicros: Int64(CMTimeGetSeconds(pts) * 1_000_000))
        }
        server.onClientsChanged = { [weak self] n in
            DispatchQueue.main.async { self?.clients = n }
        }
        server.onNeedKeyframe = { [weak self] in self?.encoder.forceKeyframe() }
        server.onCommand = { [weak self] cmd in self?.handle(cmd) }
        server.helloProvider = { [weak self] in self?.statusDict() ?? [:] }
        server.start()

        AVCaptureDevice.requestAccess(for: .video) { granted in
            guard granted else {
                DispatchQueue.main.async { self.status = "Kamera izni verilmedi. Ayarlar → GimbalCam → Kamera." }
                return
            }
            self.sessionQueue.async {
                self.configureSession()
                self.session.startRunning()
            }
        }

        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.ipAddress = NetworkInfo.wifiAddress() ?? "—"
        }
    }

    // MARK: - Configuration

    func setMode(_ m: StreamMode) {
        guard !isRecording else { status = "Kayıt sırasında mod değiştirilemez."; return }
        mode = m
        bitrateMbps = m.defaultBitrate
        sessionQueue.async { self.configureSession() }
    }

    func setLens(_ l: Lens) {
        guard !isRecording else { status = "Kayıt sırasında lens değiştirilemez."; return }
        lens = l
        sessionQueue.async { self.configureSession() }
    }

    /// Runs on sessionQueue.
    private func configureSession() {
        let mode = DispatchQueue.main.sync { self.mode }
        let lens = DispatchQueue.main.sync { self.lens }

        session.beginConfiguration()
        session.sessionPreset = .inputPriority

        for input in session.inputs { session.removeInput(input) }

        guard let dev = AVCaptureDevice.default(lens.deviceType, for: .video, position: .back)
                ?? AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) else {
            session.commitConfiguration()
            setStatus("Kamera bulunamadı.")
            return
        }
        do {
            let input = try AVCaptureDeviceInput(device: dev)
            if session.canAddInput(input) { session.addInput(input) }
        } catch {
            session.commitConfiguration()
            setStatus("Kamera açılamadı: \(error.localizedDescription)")
            return
        }

        if !session.outputs.contains(output) {
            output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange]
            output.alwaysDiscardsLateVideoFrames = true
            output.setSampleBufferDelegate(self, queue: videoQueue)
            if session.canAddOutput(output) { session.addOutput(output) }
        }

        // Pick a format matching the requested size and frame rate.
        let candidates = dev.formats.filter { f in
            let d = CMVideoFormatDescriptionGetDimensions(f.formatDescription)
            return d.width == mode.width && d.height == mode.height
        }
        let byFps = candidates.filter { f in
            f.videoSupportedFrameRateRanges.contains { $0.maxFrameRate >= Double(mode.fps) - 0.5 }
        }
        let preferred = byFps.first { f in
            CMFormatDescriptionGetMediaSubType(f.formatDescription) == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        } ?? byFps.first

        var fps = mode.fps
        var chosen = preferred
        if chosen == nil {
            chosen = candidates.max { a, b in
                (a.videoSupportedFrameRateRanges.map(\.maxFrameRate).max() ?? 0) <
                (b.videoSupportedFrameRateRanges.map(\.maxFrameRate).max() ?? 0)
            }
            if let c = chosen {
                fps = Int(c.videoSupportedFrameRateRanges.map(\.maxFrameRate).max() ?? 30)
                setStatus("\(lens.title) lensi \(mode.name) desteklemiyor, \(fps) fps kullanılıyor.")
            } else {
                setStatus("\(lens.title) lensi bu çözünürlüğü desteklemiyor.")
            }
        } else {
            setStatus("")
        }

        do {
            try dev.lockForConfiguration()
            if let chosen { dev.activeFormat = chosen }
            let dur = CMTime(value: 1, timescale: CMTimeScale(fps))
            dev.activeVideoMinFrameDuration = dur
            dev.activeVideoMaxFrameDuration = dur
            if dev.isFocusModeSupported(.continuousAutoFocus) { dev.focusMode = .continuousAutoFocus }
            if dev.isExposureModeSupported(.continuousAutoExposure) { dev.exposureMode = .continuousAutoExposure }
            if dev.isSmoothAutoFocusSupported { dev.isSmoothAutoFocusEnabled = true }
            dev.unlockForConfiguration()
        } catch {
            setStatus("Kamera ayarlanamadı: \(error.localizedDescription)")
        }

        if let conn = output.connection(with: .video) {
            if conn.isVideoStabilizationSupported { conn.preferredVideoStabilizationMode = .off }
            if conn.isVideoRotationAngleSupported(0) { conn.videoRotationAngle = 0 }
        }

        session.commitConfiguration()
        device = dev

        let dims = CMVideoFormatDescriptionGetDimensions(dev.activeFormat.formatDescription)
        encoder.configure(width: dims.width, height: dims.height, fps: fps, bitrateMbps: bitrateMbps)
        recorder.configure(width: Int(dims.width), height: Int(dims.height), fps: fps)
        server.broadcastJSON(statusDict())
    }

    private func setStatus(_ s: String) {
        DispatchQueue.main.async { self.status = s }
    }

    private func statusDict() -> [String: Any] {
        let read = { (self.mode, self.lens, self.isRecording) }
        let (m, l, r) = Thread.isMainThread ? read() : DispatchQueue.main.sync(execute: read)
        return [
            "type": "status",
            "mode": m.name, "width": m.width, "height": m.height, "fps": m.fps,
            "lens": l.rawValue, "bitrate": bitrateMbps, "recording": r,
            "device": UIDevice.current.name,
        ]
    }

    // MARK: - Remote commands (from the PC)

    private func handle(_ c: [String: Any]) {
        switch c["cmd"] as? String {
        case "mode":
            if let v = c["value"] as? String, let m = StreamMode.all.first(where: { $0.name == v }) { setMode(m) }
        case "lens":
            if let v = c["value"] as? String, let l = Lens(rawValue: v) { setLens(l) }
        case "bitrate":
            if let v = c["value"] as? Double {
                bitrateMbps = max(2, min(100, v))
                sessionQueue.async { self.configureSession() }
            }
        case "zoom":
            if let v = c["value"] as? Double { setZoom(CGFloat(v)) }
        case "focus":
            if let x = c["x"] as? Double, let y = c["y"] as? Double { focus(at: CGPoint(x: x, y: y)) }
        case "autofocus":
            sessionQueue.async {
                guard let d = self.device, (try? d.lockForConfiguration()) != nil else { return }
                if d.isFocusModeSupported(.continuousAutoFocus) { d.focusMode = .continuousAutoFocus }
                if d.isExposureModeSupported(.continuousAutoExposure) { d.exposureMode = .continuousAutoExposure }
                d.unlockForConfiguration()
            }
        case "record":
            let want = (c["value"] as? Bool) ?? !isRecording
            if want != isRecording { toggleRecording() }
        case "keyframe":
            encoder.forceKeyframe()
        case "status":
            server.broadcastJSON(statusDict())
        default:
            break
        }
    }

    private func setZoom(_ z: CGFloat) {
        sessionQueue.async {
            guard let d = self.device, (try? d.lockForConfiguration()) != nil else { return }
            d.videoZoomFactor = max(d.minAvailableVideoZoomFactor, min(z, min(d.maxAvailableVideoZoomFactor, 10)))
            d.unlockForConfiguration()
        }
    }

    private func focus(at p: CGPoint) {
        sessionQueue.async {
            guard let d = self.device, (try? d.lockForConfiguration()) != nil else { return }
            if d.isFocusPointOfInterestSupported {
                d.focusPointOfInterest = p
                d.focusMode = .autoFocus
            }
            if d.isExposurePointOfInterestSupported {
                d.exposurePointOfInterest = p
                d.exposureMode = .continuousAutoExposure
            }
            d.unlockForConfiguration()
        }
    }

    // MARK: - Recording

    func toggleRecording() {
        if isRecording {
            isRecording = false
            recordTimer?.invalidate()
            recorder.stop { [weak self] url in
                guard let self, let url else { return }
                DispatchQueue.main.async { self.status = "Kayıt bitti: \(url.lastPathComponent)" }
                self.server.sendFile(url)
            }
        } else {
            do {
                try recorder.start()
                isRecording = true
                recordStart = Date()
                recordTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
                    guard let self, let s = self.recordStart else { return }
                    let t = Int(Date().timeIntervalSince(s))
                    self.recordingTime = String(format: "%02d:%02d", t / 60, t % 60)
                }
            } catch {
                status = "Kayıt başlatılamadı: \(error.localizedDescription)"
            }
        }
        server.broadcastJSON(["type": "recording", "value": isRecording])
    }

    // MARK: - Frames

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        recorder.append(sampleBuffer)
        if server.clientCount > 0 {
            encoder.encode(sampleBuffer)
        }

        frameCount += 1
        let now = CACurrentMediaTime()
        if now - fpsWindowStart >= 1 {
            let fps = Double(frameCount) / (now - fpsWindowStart)
            frameCount = 0
            fpsWindowStart = now
            DispatchQueue.main.async { self.fpsActual = fps }
        }
    }
}
