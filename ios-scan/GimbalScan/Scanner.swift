import ARKit
import Foundation
import RealityKit
import RoomPlan
import simd

/// Two scan modes, both streamed live to the PC:
///  - room: Apple RoomPlan (walls, doors, windows, furniture with real dimensions) + USDZ export
///  - mesh: raw LiDAR scene reconstruction mesh (ARMeshAnchor) for a detailed 3D surface
final class Scanner: NSObject, ObservableObject, RoomCaptureSessionDelegate, ARSessionDelegate {
    enum Mode: String, CaseIterable { case room, mesh }

    @Published var mode: Mode = .room
    @Published var scanning = false
    @Published var busy = false          // processing / exporting
    @Published var instruction = ""      // RoomPlan coaching key
    @Published var message = ""

    let link: Link
    let arView: ARView

    private var roomSession: RoomCaptureSession?
    private var meshSession: ARSession?
    private var poseTimer: Timer?
    private var lastRoomSend = Date.distantPast
    private var anchorSentAt: [UUID: Date] = [:]
    private let encodeQueue = DispatchQueue(label: "mesh.encode", qos: .utility)

    init(link: Link) {
        self.link = link
        arView = ARView(frame: .zero, cameraMode: .ar, automaticallyConfigureSession: false)
        arView.renderOptions.insert(.disableMotionBlur)
        super.init()
    }

    var supported: Bool { RoomCaptureSession.isSupported && ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) }

    // MARK: - Control

    func start(_ m: Mode? = nil) {
        if let m { mode = m }
        stopSessions()
        anchorSentAt.removeAll()
        switch mode {
        case .room:
            let s = RoomCaptureSession()
            s.delegate = self
            roomSession = s
            arView.session = s.arSession
            arView.debugOptions.remove(.showSceneUnderstanding)
            s.run(configuration: RoomCaptureSession.Configuration())
        case .mesh:
            let s = ARSession()
            s.delegate = self
            meshSession = s
            arView.session = s
            let c = ARWorldTrackingConfiguration()
            c.sceneReconstruction = .mesh
            c.environmentTexturing = .none
            arView.debugOptions.insert(.showSceneUnderstanding)
            s.run(c, options: [.resetTracking, .removeExistingAnchors])
        }
        scanning = true
        message = ""
        link.send(json: ["type": "reset", "mode": mode.rawValue])
        sendState()
        poseTimer?.invalidate()
        poseTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in self?.sendPose() }
    }

    /// Stops scanning. In room mode, builds the final model and sends the USDZ to the PC.
    func stop() {
        guard scanning else { return }
        scanning = false
        poseTimer?.invalidate()
        switch mode {
        case .room:
            busy = true
            roomSession?.stop()      // -> captureSession(_:didEndWith:error:)
        case .mesh:
            meshSession?.pause()
            link.send(json: ["type": "done", "mode": "mesh"])
        }
        sendState()
    }

    private func stopSessions() {
        roomSession?.stop()
        roomSession = nil
        meshSession?.pause()
        meshSession = nil
        poseTimer?.invalidate()
    }

    func sendState() {
        link.send(json: ["type": "state", "scanning": scanning, "busy": busy, "mode": mode.rawValue,
                         "supported": supported])
    }

    // MARK: - Pose (drives the gimbal sweep on the PC)

    private func sendPose() {
        guard let frame = arView.session.currentFrame else { return }
        var track = "normal"
        switch frame.camera.trackingState {
        case .notAvailable: track = "na"
        case .limited: track = "limited"
        case .normal: track = "normal"
        }
        link.send(json: ["type": "pose", "t": Scanner.matrix(frame.camera.transform), "track": track])
    }

    // MARK: - RoomPlan

    func captureSession(_ session: RoomCaptureSession, didUpdate room: CapturedRoom) {
        let now = Date()
        guard now.timeIntervalSince(lastRoomSend) > 0.5 else { return }
        lastRoomSend = now
        link.send(json: Scanner.roomDict(room, final: false))
    }

    func captureSession(_ session: RoomCaptureSession, didProvide instruction: RoomCaptureSession.Instruction) {
        let key: String
        switch instruction {
        case .moveCloseToWall: key = "move_close"
        case .moveAwayFromWall: key = "move_away"
        case .slowDown: key = "slow_down"
        case .turnOnLight: key = "more_light"
        case .lowTexture: key = "low_texture"
        case .normal: key = ""
        @unknown default: key = ""
        }
        DispatchQueue.main.async { self.instruction = key }
        link.send(json: ["type": "instruction", "value": key])
    }

    func captureSession(_ session: RoomCaptureSession, didEndWith data: CapturedRoomData, error: Error?) {
        if let error {
            DispatchQueue.main.async { self.busy = false; self.message = error.localizedDescription }
            sendState()
            return
        }
        Task {
            do {
                let room = try await RoomBuilder(options: [.beautifyObjects]).capturedRoom(from: data)
                self.link.send(json: Scanner.roomDict(room, final: true))
                let df = DateFormatter()
                df.dateFormat = "yyyyMMdd_HHmmss"
                let url = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
                    .appendingPathComponent("Oda_\(df.string(from: Date())).usdz")
                try room.export(to: url)
                self.link.sendFile(url) { ok in
                    DispatchQueue.main.async {
                        self.busy = false
                        self.message = ok ? "sent" : "send_failed"
                        self.sendState()
                    }
                }
            } catch {
                await MainActor.run {
                    self.busy = false
                    self.message = error.localizedDescription
                    self.sendState()
                }
            }
        }
    }

    static func roomDict(_ room: CapturedRoom, final: Bool) -> [String: Any] {
        func surface(_ s: CapturedRoom.Surface) -> [String: Any] {
            var d: [String: Any] = [
                "dims": [Double(s.dimensions.x), Double(s.dimensions.y), Double(s.dimensions.z)],
                "t": matrix(s.transform),
            ]
            if !s.polygonCorners.isEmpty {
                d["poly"] = s.polygonCorners.map { [Double($0.x), Double($0.y), Double($0.z)] }
            }
            if case .door(let isOpen) = s.category { d["open"] = isOpen }
            return d
        }
        func object(_ o: CapturedRoom.Object) -> [String: Any] {
            [
                "cat": String(describing: o.category),
                "dims": [Double(o.dimensions.x), Double(o.dimensions.y), Double(o.dimensions.z)],
                "t": matrix(o.transform),
            ]
        }
        return [
            "type": "room",
            "final": final,
            "walls": room.walls.map(surface),
            "doors": room.doors.map(surface),
            "windows": room.windows.map(surface),
            "openings": room.openings.map(surface),
            "floors": room.floors.map(surface),
            "objects": room.objects.map(object),
        ]
    }

    // MARK: - LiDAR mesh

    func session(_ session: ARSession, didAdd anchors: [ARAnchor]) { handle(anchors) }
    func session(_ session: ARSession, didUpdate anchors: [ARAnchor]) { handle(anchors) }

    func session(_ session: ARSession, didRemove anchors: [ARAnchor]) {
        for a in anchors where a is ARMeshAnchor {
            var d = Data([3])
            d.append(Scanner.uuidBytes(a.identifier))
            link.send(data: d)
            anchorSentAt[a.identifier] = nil
        }
    }

    private func handle(_ anchors: [ARAnchor]) {
        guard scanning, mode == .mesh else { return }
        let now = Date()
        for case let a as ARMeshAnchor in anchors {
            if let t = anchorSentAt[a.identifier], now.timeIntervalSince(t) < 1.0 { continue }
            if link.backlog > 6_000_000 { return }   // Wi-Fi is behind; try again on the next update
            anchorSentAt[a.identifier] = now
            encodeQueue.async { [weak self] in
                guard let self else { return }
                self.link.send(data: Scanner.encode(a))
            }
        }
    }

    static func encode(_ a: ARMeshAnchor) -> Data {
        let g = a.geometry
        let vCount = g.vertices.count
        let fCount = g.faces.count

        var verts = [Float](repeating: 0, count: vCount * 3)
        let vb = g.vertices.buffer.contents().advanced(by: g.vertices.offset)
        let vStride = g.vertices.stride
        for i in 0..<vCount {
            let p = vb.advanced(by: i * vStride).assumingMemoryBound(to: Float.self)
            verts[i * 3] = p[0]
            verts[i * 3 + 1] = p[1]
            verts[i * 3 + 2] = p[2]
        }

        let idxCount = fCount * g.faces.indexCountPerPrimitive
        var faces = [UInt32](repeating: 0, count: idxCount)
        let fb = g.faces.buffer.contents()
        if g.faces.bytesPerIndex == 4 {
            let p = fb.assumingMemoryBound(to: UInt32.self)
            for i in 0..<idxCount { faces[i] = p[i] }
        } else {
            let p = fb.assumingMemoryBound(to: UInt16.self)
            for i in 0..<idxCount { faces[i] = UInt32(p[i]) }
        }

        var d = Data(capacity: 1 + 16 + 64 + 8 + verts.count * 4 + faces.count * 4)
        d.append(1)
        d.append(uuidBytes(a.identifier))
        let m = matrix(a.transform).map { Float($0) }
        m.withUnsafeBytes { d.append(contentsOf: $0) }
        var vc = UInt32(vCount).littleEndian
        var fc = UInt32(fCount).littleEndian
        withUnsafeBytes(of: &vc) { d.append(contentsOf: $0) }
        withUnsafeBytes(of: &fc) { d.append(contentsOf: $0) }
        verts.withUnsafeBytes { d.append(contentsOf: $0) }
        faces.withUnsafeBytes { d.append(contentsOf: $0) }
        return d
    }

    // MARK: - Helpers

    /// Column-major 16 values (matches three.js Matrix4.fromArray).
    static func matrix(_ m: simd_float4x4) -> [Double] {
        [m.columns.0, m.columns.1, m.columns.2, m.columns.3].flatMap { [Double($0.x), Double($0.y), Double($0.z), Double($0.w)] }
    }

    static func uuidBytes(_ id: UUID) -> Data {
        var u = id.uuid
        return withUnsafeBytes(of: &u) { Data($0) }
    }
}
